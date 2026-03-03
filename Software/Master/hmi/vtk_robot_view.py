"""High quality VTK robot viewer for Quick Move page."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pybullet as pb
import vtk
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
from vtkmodules.util import numpy_support


@dataclass
class VisualMesh:
    link_name: str
    mesh_path: Path
    color_rgb: Tuple[float, float, float]
    origin_xyz: np.ndarray
    origin_rpy: np.ndarray
    scale_xyz: np.ndarray


def _parse_floats(text: str | None, length: int, default: float) -> np.ndarray:
    if not text:
        return np.full(length, default, dtype=float)
    vals = []
    for token in text.replace(",", " ").split():
        try:
            vals.append(float(token))
        except Exception:
            vals.append(default)
    if len(vals) < length:
        vals.extend([default] * (length - len(vals)))
    return np.array(vals[:length], dtype=float)


def _rpy_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]], dtype=float)
    ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=float)
    rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=float)
    return rz @ ry @ rx


def _vtk_matrix_from_pose(pos: Sequence[float], quat: Sequence[float]) -> vtk.vtkMatrix4x4:
    rot = np.array(pb.getMatrixFromQuaternion(quat), dtype=float).reshape(3, 3)
    mat = vtk.vtkMatrix4x4()
    for r in range(3):
        for c in range(3):
            mat.SetElement(r, c, float(rot[r, c]))
        mat.SetElement(r, 3, float(pos[r]))
    mat.SetElement(3, 0, 0.0)
    mat.SetElement(3, 1, 0.0)
    mat.SetElement(3, 2, 0.0)
    mat.SetElement(3, 3, 1.0)
    return mat


class VtkRobotView(QWidget):
    """VTK-based robot view with configurable visual presets."""

    def __init__(self, urdf_path: Path):
        super().__init__()
        self.urdf_path = Path(urdf_path)

        self.pb_client = None
        self.pb_robot_id = None
        self.base_link_name = "base"
        self.joint_indices: List[int] = []
        self.joint_names: List[str] = []
        self.joint_limits: List[Tuple[float, float]] = []
        self.joint_values: List[float] = []
        self._child_link_to_joint: Dict[str, int] = {}
        self._ee_link_index: Optional[int] = None
        self._urdf_joint_order: List[Tuple[str, str]] = []
        self._eye_in_hand_link_name: Optional[str] = None
        self._eye_in_hand_link_index: Optional[int] = None

        self._actors_by_link: Dict[str, List[vtk.vtkActor]] = {}
        self._eye_actors_by_link: Dict[str, List[vtk.vtkActor]] = {}
        self._mesh_actors: List[vtk.vtkActor] = []
        self._eye_mesh_actors: List[vtk.vtkActor] = []
        self._normal_filters: List[vtk.vtkPolyDataNormals] = []
        self._joint_label_actors: List[Tuple[int, vtk.vtkBillboardTextActor3D]] = []
        self._lights: List[vtk.vtkLight] = []
        self._eye_lights: List[vtk.vtkLight] = []
        self._floor_actor: vtk.vtkActor | None = None
        self._ground_line_actor: vtk.vtkActor | None = None
        self._eye_floor_actor: vtk.vtkActor | None = None
        self._eye_ground_line_actor: vtk.vtkActor | None = None
        self._scene_axes_actor: vtk.vtkAxesActor | None = None
        self._apple_actor: vtk.vtkActor | None = None
        self._apple_eye_actor: vtk.vtkActor | None = None
        self._eye_renderer: vtk.vtkRenderer | None = None
        self._eye_render_window: vtk.vtkRenderWindow | None = None
        self._eye_camera: vtk.vtkCamera | None = None
        self._eye_w2i: vtk.vtkWindowToImageFilter | None = None

        self._antialias_mode = "off"
        self._antialias_samples = 0
        self._material_preset = "default"
        self._background_theme = "light"
        self._ui_theme = "light"
        self._camera_preset = "iso"
        self._mesh_smoothing_enabled = True
        self._mesh_feature_angle = 55.0

        self._build_widget()
        visuals = self._load_visuals_from_urdf()
        self._init_pybullet_kinematics()
        self._init_eye_in_hand_pipeline()
        self._add_floor()
        self._add_lights()
        self._add_mesh_actors(visuals)
        self._add_apple_model()
        self._add_orientation_axes()

        self.set_background("studio")
        self.set_mesh_smoothing(enabled=True, feature_angle=55.0)
        self.set_material_preset("soft")
        # Compatibility-first default: FXAA is visually good and avoids some MSAA black-screen drivers.
        self.set_antialiasing(mode="fxaa", samples=0, announce=True)
        self.set_camera_preset("iso")

        self._update_actor_poses()
        self.interactor.Initialize()
        self.render_window.Render()

    def _build_widget(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.vtk_widget = QVTKRenderWindowInteractor(self)
        layout.addWidget(self.vtk_widget)

        self.render_window = self.vtk_widget.GetRenderWindow()
        # Do not force alpha/stencil buffers here: on some drivers this triggers black rendering.
        self.render_window.SetMultiSamples(0)

        self.renderer = vtk.vtkRenderer()
        self.render_window.AddRenderer(self.renderer)

        self.interactor = self.render_window.GetInteractor()
        self.interactor.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())

    def _init_eye_in_hand_pipeline(self):
        self._eye_renderer = vtk.vtkRenderer()
        self._eye_renderer.SetBackground(0.05, 0.06, 0.08)
        self._eye_renderer.GradientBackgroundOff()
        self._eye_renderer.AutomaticLightCreationOn()

        self._eye_render_window = vtk.vtkRenderWindow()
        self._eye_render_window.SetOffScreenRendering(1)
        self._eye_render_window.SetMultiSamples(0)
        self._eye_render_window.SetSize(960, 720)
        self._eye_render_window.AddRenderer(self._eye_renderer)

        self._eye_camera = vtk.vtkCamera()
        self._eye_camera.SetViewAngle(62.0)
        self._eye_renderer.SetActiveCamera(self._eye_camera)

        self._eye_w2i = vtk.vtkWindowToImageFilter()
        self._eye_w2i.SetInput(self._eye_render_window)
        self._eye_w2i.SetInputBufferTypeToRGB()
        self._eye_w2i.ReadFrontBufferOff()

    @staticmethod
    def _copy_actor_property(src: vtk.vtkActor, dst: vtk.vtkActor):
        src_prop = src.GetProperty()
        dst_prop = dst.GetProperty()
        if src_prop is None or dst_prop is None:
            return
        dst_prop.DeepCopy(src_prop)

    def _resolve_apple_mesh_path(self) -> Optional[Path]:
        candidates = [
            self.urdf_path.parent.parent / "apple" / "Apple.stl",
            Path(__file__).resolve().parents[3] / "sdk" / "src" / "soarmMoce_sdk" / "resources" / "apple" / "Apple.stl",
        ]
        for path in candidates:
            if path.exists():
                return path.resolve()
        return None

    @staticmethod
    def _estimate_mesh_scale(bounds: Sequence[float]) -> float:
        if len(bounds) < 6:
            return 1.0
        span = max(float(bounds[1] - bounds[0]), float(bounds[3] - bounds[2]), float(bounds[5] - bounds[4]))
        if span > 2.0:
            return 0.001
        if span > 0.3:
            return 0.01
        return 1.0

    def _add_apple_model(self):
        apple_path = self._resolve_apple_mesh_path()
        if apple_path is None:
            return

        reader = vtk.vtkSTLReader()
        reader.SetFileName(str(apple_path))
        reader.Update()
        output = reader.GetOutput()
        if output is None or output.GetNumberOfPoints() == 0:
            return

        scale = self._estimate_mesh_scale(output.GetBounds())
        tf = vtk.vtkTransform()
        tf.Scale(scale, scale, scale)

        tf_filter = vtk.vtkTransformPolyDataFilter()
        tf_filter.SetTransform(tf)
        tf_filter.SetInputConnection(reader.GetOutputPort())

        normals = vtk.vtkPolyDataNormals()
        normals.SetInputConnection(tf_filter.GetOutputPort())
        normals.ComputePointNormalsOn()
        normals.ComputeCellNormalsOff()
        normals.ConsistencyOn()
        normals.AutoOrientNormalsOff()
        normals.SetFeatureAngle(self._mesh_feature_angle)
        normals.SplittingOn()
        self._normal_filters.append(normals)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(normals.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.SetPosition(0.30, 0.00, 0.05)
        prop = actor.GetProperty()
        prop.SetColor(1.0, 0.0, 0.0)
        prop.SetInterpolationToPhong()
        prop.SetAmbient(0.24)
        prop.SetDiffuse(0.74)
        prop.SetSpecular(0.18)
        prop.SetSpecularPower(22.0)
        self.renderer.AddActor(actor)
        self._apple_actor = actor
        self._mesh_actors.append(actor)

        if self._eye_renderer is not None:
            eye_actor = vtk.vtkActor()
            eye_actor.SetMapper(mapper)
            eye_actor.SetPosition(0.30, 0.00, 0.05)
            self._copy_actor_property(actor, eye_actor)
            self._eye_renderer.AddActor(eye_actor)
            self._apple_eye_actor = eye_actor
            self._eye_mesh_actors.append(eye_actor)

    def _resolve_mesh_path(self, mesh_filename: str) -> Path:
        path = Path(mesh_filename)
        if path.is_absolute():
            return path
        return (self.urdf_path.parent / path).resolve()

    def _load_visuals_from_urdf(self) -> List[VisualMesh]:
        if not self.urdf_path.exists():
            raise FileNotFoundError(f"URDF not found: {self.urdf_path}")

        tree = ET.parse(str(self.urdf_path))
        root = tree.getroot()

        all_links = []
        child_links = set()
        self._urdf_joint_order = []
        materials: Dict[str, Tuple[float, float, float]] = {}
        visuals: List[VisualMesh] = []

        for mat_node in root.findall("material"):
            name = mat_node.get("name")
            if not name:
                continue
            color_node = mat_node.find("color")
            if color_node is None:
                continue
            rgba = _parse_floats(color_node.get("rgba"), 4, 1.0)
            materials[name] = (float(rgba[0]), float(rgba[1]), float(rgba[2]))

        for joint_node in root.findall("joint"):
            joint_name = str(joint_node.get("name") or "")
            child_node = joint_node.find("child")
            child_link = child_node.get("link") if child_node is not None else None
            if child_link:
                child_links.add(child_link)
                self._urdf_joint_order.append((joint_name, str(child_link)))

        for link_node in root.findall("link"):
            link_name = link_node.get("name")
            if not link_name:
                continue
            all_links.append(link_name)

            for visual_node in link_node.findall("visual"):
                geom = visual_node.find("geometry")
                mesh_node = geom.find("mesh") if geom is not None else None
                if mesh_node is None:
                    continue
                mesh_filename = mesh_node.get("filename")
                if not mesh_filename:
                    continue

                origin_node = visual_node.find("origin")
                origin_xyz = _parse_floats(origin_node.get("xyz") if origin_node is not None else None, 3, 0.0)
                origin_rpy = _parse_floats(origin_node.get("rpy") if origin_node is not None else None, 3, 0.0)
                scale_xyz = _parse_floats(mesh_node.get("scale"), 3, 1.0)

                color = (0.92, 0.92, 0.95)
                material_node = visual_node.find("material")
                if material_node is not None:
                    inline_color = material_node.find("color")
                    if inline_color is not None:
                        rgba = _parse_floats(inline_color.get("rgba"), 4, 1.0)
                        color = (float(rgba[0]), float(rgba[1]), float(rgba[2]))
                    else:
                        mat_name = material_node.get("name")
                        if mat_name in materials:
                            color = materials[mat_name]

                visuals.append(
                    VisualMesh(
                        link_name=link_name,
                        mesh_path=self._resolve_mesh_path(mesh_filename),
                        color_rgb=color,
                        origin_xyz=origin_xyz,
                        origin_rpy=origin_rpy,
                        scale_xyz=scale_xyz,
                    )
                )

        roots = [name for name in all_links if name not in child_links]
        if roots:
            self.base_link_name = roots[0]
        elif all_links:
            self.base_link_name = all_links[0]

        return visuals

    def _init_pybullet_kinematics(self):
        self.pb_client = pb.connect(pb.DIRECT)
        self.pb_robot_id = pb.loadURDF(
            str(self.urdf_path),
            basePosition=[0, 0, 0],
            baseOrientation=[0, 0, 0, 1],
            useFixedBase=True,
            physicsClientId=self.pb_client,
        )

        self.joint_indices = []
        self.joint_names = []
        self.joint_limits = []
        self.joint_values = []
        self._child_link_to_joint = {}

        num_joints = pb.getNumJoints(self.pb_robot_id, physicsClientId=self.pb_client)
        for idx in range(num_joints):
            info = pb.getJointInfo(self.pb_robot_id, idx, physicsClientId=self.pb_client)
            joint_type = info[2]
            link_name = info[12].decode("utf-8")
            self._child_link_to_joint[link_name] = idx

            if joint_type not in (pb.JOINT_REVOLUTE, pb.JOINT_PRISMATIC):
                continue

            name = info[1].decode("utf-8")
            lo, hi = float(info[8]), float(info[9])
            if (not math.isfinite(lo)) or (not math.isfinite(hi)) or lo >= hi:
                lo, hi = -math.pi, math.pi
            q0 = float(np.clip(0.0, lo, hi))
            pb.resetJointState(self.pb_robot_id, idx, q0, physicsClientId=self.pb_client)

            self.joint_indices.append(idx)
            self.joint_names.append(name)
            self.joint_limits.append((lo, hi))
            self.joint_values.append(q0)

        if not self.joint_indices:
            raise RuntimeError("No movable joints found in URDF")
        self._ee_link_index = self._detect_end_effector_index()
        self._eye_in_hand_link_name = self._detect_eye_in_hand_link_name()
        if self._eye_in_hand_link_name:
            self._eye_in_hand_link_index = self._child_link_to_joint.get(self._eye_in_hand_link_name)
        if self._eye_in_hand_link_index is None:
            self._eye_in_hand_link_index = self._ee_link_index

    def _detect_eye_in_hand_link_name(self) -> Optional[str]:
        if not self._urdf_joint_order:
            return None

        preferred_joint_names = (
            "wrist_roll",
            
        )
        for joint_name, child_link in self._urdf_joint_order:
            if joint_name.strip().lower() in preferred_joint_names:
                return child_link

        preferred_link_names = (
            "wrist_roll",
            
        )
        for _joint_name, child_link in self._urdf_joint_order:
            if child_link.strip().lower() in preferred_link_names:
                return child_link

        return self._urdf_joint_order[-1][1]

    def _detect_end_effector_index(self) -> Optional[int]:
        if self.pb_client is None or self.pb_robot_id is None:
            return None
        try:
            num_joints = pb.getNumJoints(self.pb_robot_id, physicsClientId=self.pb_client)
            if num_joints <= 0:
                return None

            wrist_roll_idx = None
            parents = set()
            for ji in range(num_joints):
                info = pb.getJointInfo(self.pb_robot_id, ji, physicsClientId=self.pb_client)
                joint_name = info[1].decode("utf-8")
                if joint_name == "wrist_roll":
                    wrist_roll_idx = ji
                parent_idx = int(info[16])
                if parent_idx >= 0:
                    parents.add(parent_idx)

            if wrist_roll_idx is not None:
                return wrist_roll_idx

            leaves = sorted(set(range(num_joints)) - parents)
            if leaves:
                return leaves[-1]
            return num_joints - 1
        except Exception:
            return None

    def get_end_effector_pose_base(self) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        if self.pb_client is None or self.pb_robot_id is None:
            return None
        if self._ee_link_index is None:
            self._ee_link_index = self._detect_end_effector_index()
        if self._ee_link_index is None:
            return None

        state = pb.getLinkState(
            self.pb_robot_id,
            int(self._ee_link_index),
            computeForwardKinematics=True,
            physicsClientId=self.pb_client,
        )
        xyz = np.array(state[4], dtype=float)
        rpy = np.array(pb.getEulerFromQuaternion(state[5]), dtype=float)
        return xyz, rpy

    def _update_eye_in_hand_camera_pose(self):
        if self.pb_client is None or self.pb_robot_id is None:
            return
        if self._eye_camera is None:
            return

        link_idx = self._eye_in_hand_link_index
        if link_idx is None:
            link_idx = self._ee_link_index
        if link_idx is None:
            return

        state = pb.getLinkState(
            self.pb_robot_id,
            int(link_idx),
            computeForwardKinematics=True,
            physicsClientId=self.pb_client,
        )
        link_pos = np.asarray(state[4], dtype=float)
        link_quat = state[5]
        rot = np.asarray(pb.getMatrixFromQuaternion(link_quat), dtype=float).reshape(3, 3)

        # Eye-in-hand camera: offset slightly above the wrist frame and look forward.
        cam_pos = link_pos + rot @ np.array([0.060, 0.000, 0.028], dtype=float)
        focal = cam_pos + rot @ np.array([0.320, 0.000, 0.000], dtype=float)
        view_up = rot @ np.array([0.000, 0.000, 1.000], dtype=float)

        self._eye_camera.SetPosition(float(cam_pos[0]), float(cam_pos[1]), float(cam_pos[2]))
        self._eye_camera.SetFocalPoint(float(focal[0]), float(focal[1]), float(focal[2]))
        self._eye_camera.SetViewUp(float(view_up[0]), float(view_up[1]), float(view_up[2]))
        self._eye_camera.SetParallelProjection(False)
        self._eye_camera.SetViewAngle(62.0)
        if self._eye_renderer is not None:
            self._eye_renderer.ResetCameraClippingRange()

    def render_eye_in_hand_frame(self, width: int = 960, height: int = 720) -> Optional[np.ndarray]:
        if self._eye_renderer is None or self._eye_render_window is None or self._eye_w2i is None:
            return None

        w = max(160, int(width))
        h = max(120, int(height))

        self._update_eye_in_hand_camera_pose()
        self._eye_render_window.SetSize(w, h)
        self._eye_render_window.Render()
        self._eye_w2i.Modified()
        self._eye_w2i.Update()

        image = self._eye_w2i.GetOutput()
        if image is None:
            return None
        dims = image.GetDimensions()
        if dims is None or dims[0] <= 0 or dims[1] <= 0:
            return None

        scalars = image.GetPointData().GetScalars()
        if scalars is None or scalars.GetNumberOfComponents() < 3:
            return None

        frame = numpy_support.vtk_to_numpy(scalars)
        h_img, w_img = int(dims[1]), int(dims[0])
        frame = frame.reshape(h_img, w_img, scalars.GetNumberOfComponents())
        rgb = np.ascontiguousarray(np.flipud(frame[:, :, :3]))
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        return bgr

    def _make_local_visual_filter(self, reader_output_port, visual: VisualMesh):
        rot = _rpy_matrix(float(visual.origin_rpy[0]), float(visual.origin_rpy[1]), float(visual.origin_rpy[2]))
        rot_scaled = rot @ np.diag(visual.scale_xyz)

        tf = vtk.vtkTransform()
        mat = vtk.vtkMatrix4x4()
        for r in range(3):
            for c in range(3):
                mat.SetElement(r, c, float(rot_scaled[r, c]))
            mat.SetElement(r, 3, float(visual.origin_xyz[r]))
        mat.SetElement(3, 0, 0.0)
        mat.SetElement(3, 1, 0.0)
        mat.SetElement(3, 2, 0.0)
        mat.SetElement(3, 3, 1.0)
        tf.SetMatrix(mat)

        tf_filter = vtk.vtkTransformPolyDataFilter()
        tf_filter.SetTransform(tf)
        tf_filter.SetInputConnection(reader_output_port)
        return tf_filter

    def _add_mesh_actors(self, visuals: List[VisualMesh]):
        self._actors_by_link.clear()
        self._eye_actors_by_link.clear()
        self._mesh_actors.clear()
        self._eye_mesh_actors.clear()
        self._normal_filters.clear()

        for visual in visuals:
            if not visual.mesh_path.exists():
                continue

            suffix = visual.mesh_path.suffix.lower()
            if suffix == ".stl":
                reader = vtk.vtkSTLReader()
            elif suffix == ".obj":
                reader = vtk.vtkOBJReader()
            else:
                continue

            reader.SetFileName(str(visual.mesh_path))
            reader.Update()
            if reader.GetOutput() is None or reader.GetOutput().GetNumberOfPoints() == 0:
                continue

            tf_filter = self._make_local_visual_filter(reader.GetOutputPort(), visual)

            normals = vtk.vtkPolyDataNormals()
            normals.SetInputConnection(tf_filter.GetOutputPort())
            normals.ComputePointNormalsOn()
            normals.ComputeCellNormalsOff()
            normals.ConsistencyOn()
            normals.AutoOrientNormalsOff()
            normals.SetFeatureAngle(self._mesh_feature_angle)
            normals.SplittingOn()
            self._normal_filters.append(normals)

            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(normals.GetOutputPort())

            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*visual.color_rgb)
            actor.GetProperty().SetInterpolationToPhong()

            self.renderer.AddActor(actor)
            self._mesh_actors.append(actor)
            self._actors_by_link.setdefault(visual.link_name, []).append(actor)

            if self._eye_renderer is not None:
                eye_actor = vtk.vtkActor()
                eye_actor.SetMapper(mapper)
                self._copy_actor_property(actor, eye_actor)
                self._eye_renderer.AddActor(eye_actor)
                self._eye_mesh_actors.append(eye_actor)
                self._eye_actors_by_link.setdefault(visual.link_name, []).append(eye_actor)

        for idx, _joint_idx in enumerate(self.joint_indices):
            text = vtk.vtkBillboardTextActor3D()
            text.SetInput(f"J{idx + 1}")
            prop = text.GetTextProperty()
            prop.SetFontSize(18)
            self.renderer.AddActor(text)
            self._joint_label_actors.append((_joint_idx, text))
        self._update_joint_label_style()

    def _scene_axes_text_property(self):
        if self._scene_axes_actor is None:
            return None
        try:
            cap = self._scene_axes_actor.GetXAxisCaptionActor2D()
            if cap is None:
                return None
            return cap.GetCaptionTextProperty()
        except Exception:
            return None

    @staticmethod
    def _copy_text_font_style(src_prop, dst_prop):
        if src_prop is None or dst_prop is None:
            return
        try:
            dst_prop.SetFontFamily(int(src_prop.GetFontFamily()))
        except Exception:
            pass
        try:
            dst_prop.SetBold(bool(src_prop.GetBold()))
        except Exception:
            pass
        try:
            dst_prop.SetItalic(bool(src_prop.GetItalic()))
        except Exception:
            pass
        try:
            dst_prop.SetShadow(bool(src_prop.GetShadow()))
        except Exception:
            pass

    def _update_joint_label_style(self):
        axis_text_prop = self._scene_axes_text_property()
        dark_like = self._background_theme == "dark" or self._ui_theme == "dark"
        if dark_like:
            label_color = (1.00, 1.00, 1.00)
            axis_label_color = (1.00, 1.00, 1.00)
        else:
            label_color = (0.28, 0.28, 0.30)
            axis_label_color = (0.22, 0.25, 0.30)

        if self._scene_axes_actor is not None:
            for getter in (
                self._scene_axes_actor.GetXAxisCaptionActor2D,
                self._scene_axes_actor.GetYAxisCaptionActor2D,
                self._scene_axes_actor.GetZAxisCaptionActor2D,
            ):
                try:
                    cap = getter()
                    if cap is None:
                        continue
                    cap_prop = cap.GetCaptionTextProperty()
                    if cap_prop is None:
                        continue
                    cap_prop.SetColor(*axis_label_color)
                except Exception:
                    continue

        for _, text in self._joint_label_actors:
            prop = text.GetTextProperty()
            self._copy_text_font_style(axis_text_prop, prop)
            prop.SetFontSize(18)
            prop.SetColor(*label_color)
            # Keep labels crisp on dark background; shadow can look like a black glyph.
            prop.SetShadow(False)
            text.Modified()

    def _build_ground_grid_polydata(
        self,
        radius: float = 0.80,
        circles: int = 5,
        radials: int = 24,
        circle_segments: int = 120,
        z: float = 0.0008,
    ) -> vtk.vtkPolyData:
        points = vtk.vtkPoints()
        lines = vtk.vtkCellArray()

        for ci in range(1, circles + 1):
            r = radius * (float(ci) / float(circles))
            for si in range(circle_segments):
                t0 = (2.0 * math.pi * si) / float(circle_segments)
                t1 = (2.0 * math.pi * (si + 1)) / float(circle_segments)
                p0 = points.InsertNextPoint(r * math.cos(t0), r * math.sin(t0), z)
                p1 = points.InsertNextPoint(r * math.cos(t1), r * math.sin(t1), z)
                line = vtk.vtkLine()
                line.GetPointIds().SetId(0, p0)
                line.GetPointIds().SetId(1, p1)
                lines.InsertNextCell(line)

        for ri in range(radials):
            t = (2.0 * math.pi * ri) / float(radials)
            p0 = points.InsertNextPoint(0.0, 0.0, z)
            p1 = points.InsertNextPoint(radius * math.cos(t), radius * math.sin(t), z)
            line = vtk.vtkLine()
            line.GetPointIds().SetId(0, p0)
            line.GetPointIds().SetId(1, p1)
            lines.InsertNextCell(line)

        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetLines(lines)
        return poly

    def _update_ground_style(self):
        if self._background_theme == "dark":
            floor_color = (0.31, 0.35, 0.40)
            floor_opacity = 0.42
            line_color = (0.55, 0.61, 0.68)
            line_opacity = 0.62
        else:
            floor_color = (0.78, 0.82, 0.86)
            floor_opacity = 0.62
            line_color = (0.62, 0.68, 0.73)
            line_opacity = 0.78

        if self._floor_actor is not None:
            prop = self._floor_actor.GetProperty()
            prop.SetColor(*floor_color)
            prop.SetAmbient(0.82)
            prop.SetDiffuse(0.20)
            prop.SetSpecular(0.03)
            prop.SetSpecularPower(6.0)
            prop.SetOpacity(floor_opacity)

        if self._ground_line_actor is not None:
            prop = self._ground_line_actor.GetProperty()
            prop.SetColor(*line_color)
            prop.SetLineWidth(1.1)
            prop.SetOpacity(line_opacity)
            prop.SetAmbient(0.90)
            prop.SetDiffuse(0.10)

        if self._eye_floor_actor is not None:
            prop = self._eye_floor_actor.GetProperty()
            prop.SetColor(*floor_color)
            prop.SetAmbient(0.82)
            prop.SetDiffuse(0.20)
            prop.SetSpecular(0.03)
            prop.SetSpecularPower(6.0)
            prop.SetOpacity(floor_opacity)

        if self._eye_ground_line_actor is not None:
            prop = self._eye_ground_line_actor.GetProperty()
            prop.SetColor(*line_color)
            prop.SetLineWidth(1.1)
            prop.SetOpacity(line_opacity)
            prop.SetAmbient(0.90)
            prop.SetDiffuse(0.10)

    def _add_floor(self):
        # Ground disk (transparent gray) plus radial/concentric helper lines.
        disk = vtk.vtkDiskSource()
        disk.SetInnerRadius(0.0)
        disk.SetOuterRadius(0.82)
        disk.SetRadialResolution(1)
        disk.SetCircumferentialResolution(128)

        disk_mapper = vtk.vtkPolyDataMapper()
        disk_mapper.SetInputConnection(disk.GetOutputPort())

        disk_actor = vtk.vtkActor()
        disk_actor.SetMapper(disk_mapper)
        disk_actor.SetPosition(0.0, 0.0, -0.0015)
        self.renderer.AddActor(disk_actor)
        self._floor_actor = disk_actor

        if self._eye_renderer is not None:
            eye_disk_actor = vtk.vtkActor()
            eye_disk_actor.SetMapper(disk_mapper)
            eye_disk_actor.SetPosition(0.0, 0.0, -0.0015)
            self._eye_renderer.AddActor(eye_disk_actor)
            self._eye_floor_actor = eye_disk_actor

        grid_poly = self._build_ground_grid_polydata(radius=0.80, circles=5, radials=24, circle_segments=120, z=0.0005)
        grid_mapper = vtk.vtkPolyDataMapper()
        grid_mapper.SetInputData(grid_poly)
        grid_actor = vtk.vtkActor()
        grid_actor.SetMapper(grid_mapper)
        grid_actor.SetPosition(0.0, 0.0, -0.0010)
        self.renderer.AddActor(grid_actor)
        self._ground_line_actor = grid_actor

        if self._eye_renderer is not None:
            eye_grid_actor = vtk.vtkActor()
            eye_grid_actor.SetMapper(grid_mapper)
            eye_grid_actor.SetPosition(0.0, 0.0, -0.0010)
            self._eye_renderer.AddActor(eye_grid_actor)
            self._eye_ground_line_actor = eye_grid_actor

        # Small in-scene axis near the ground; keep the corner orientation widget as well.
        axes = vtk.vtkAxesActor()
        axes.SetTotalLength(0.10, 0.10, 0.10)
        axes.SetShaftTypeToLine()
        axes.SetXAxisLabelText("X")
        axes.SetYAxisLabelText("Y")
        axes.SetZAxisLabelText("Z")
        tf = vtk.vtkTransform()
        tf.Translate(0.22, -0.22, 0.002)
        axes.SetUserTransform(tf)
        self.renderer.AddActor(axes)
        self._scene_axes_actor = axes

        self._update_ground_style()

    def _add_lights(self):
        self.renderer.AutomaticLightCreationOff()
        for light in self._lights:
            self.renderer.RemoveLight(light)
        self._lights.clear()

        if self._eye_renderer is not None:
            self._eye_renderer.AutomaticLightCreationOff()
            for light in self._eye_lights:
                self._eye_renderer.RemoveLight(light)
            self._eye_lights.clear()

        key = vtk.vtkLight()
        # Key light from +X +Y direction.
        key.SetLightTypeToSceneLight()
        key.SetPosition(1.30, 1.25, 1.55)
        key.SetFocalPoint(0.0, 0.0, 0.0)
        key.SetColor(1.0, 0.98, 0.96)
        key.SetIntensity(0.78)
        self.renderer.AddLight(key)
        self._lights.append(key)
        if self._eye_renderer is not None:
            eye_key = vtk.vtkLight()
            eye_key.DeepCopy(key)
            self._eye_renderer.AddLight(eye_key)
            self._eye_lights.append(eye_key)

        fill = vtk.vtkLight()
        fill.SetLightTypeToSceneLight()
        fill.SetPosition(0.55, 1.85, 1.10)
        fill.SetFocalPoint(0.0, 0.0, 0.2)
        fill.SetColor(0.90, 0.94, 1.0)
        fill.SetIntensity(0.34)
        self.renderer.AddLight(fill)
        self._lights.append(fill)
        if self._eye_renderer is not None:
            eye_fill = vtk.vtkLight()
            eye_fill.DeepCopy(fill)
            self._eye_renderer.AddLight(eye_fill)
            self._eye_lights.append(eye_fill)

        rim = vtk.vtkLight()
        rim.SetLightTypeToSceneLight()
        rim.SetPosition(-1.05, -1.20, 0.75)
        rim.SetFocalPoint(0.0, 0.0, 0.2)
        rim.SetColor(0.95, 0.95, 1.0)
        rim.SetIntensity(0.14)
        self.renderer.AddLight(rim)
        self._lights.append(rim)
        if self._eye_renderer is not None:
            eye_rim = vtk.vtkLight()
            eye_rim.DeepCopy(rim)
            self._eye_renderer.AddLight(eye_rim)
            self._eye_lights.append(eye_rim)

    def _add_orientation_axes(self):
        axes = vtk.vtkAxesActor()
        axes.SetTotalLength(0.08, 0.08, 0.08)
        self.axes_widget = vtk.vtkOrientationMarkerWidget()
        self.axes_widget.SetOrientationMarker(axes)
        self.axes_widget.SetInteractor(self.interactor)
        self.axes_widget.SetViewport(0.78, 0.02, 0.98, 0.22)
        self.axes_widget.SetEnabled(1)
        self.axes_widget.InteractiveOff()

    def _set_fxaa_enabled(self, enabled: bool) -> bool:
        if hasattr(self.renderer, "SetUseFXAA"):
            self.renderer.SetUseFXAA(bool(enabled))
            return True
        if enabled and hasattr(self.renderer, "UseFXAAOn"):
            self.renderer.UseFXAAOn()
            return True
        if (not enabled) and hasattr(self.renderer, "UseFXAAOff"):
            self.renderer.UseFXAAOff()
            return True
        return False

    def set_antialiasing(self, mode: str = "msaa", samples: int = 8, announce: bool = False):
        mode_norm = (mode or "off").strip().lower()
        if mode_norm in {"msaa8", "msaa_8", "msaa-x8"}:
            mode_norm = "msaa"
            samples = 8
        elif mode_norm in {"msaa4", "msaa_4", "msaa-x4"}:
            mode_norm = "msaa"
            samples = 4

        self._set_fxaa_enabled(False)
        final_mode = "off"
        final_samples = 0

        if mode_norm == "fxaa":
            if self._set_fxaa_enabled(True):
                self.render_window.SetMultiSamples(0)
                final_mode = "fxaa"
                final_samples = 0
            else:
                # Do not force MSAA fallback here; some GPUs black-screen on MSAA in Qt+VTK.
                mode_norm = "off"

        if mode_norm == "msaa":
            desired = int(samples) if samples is not None else 8
            desired = max(0, min(16, desired))
            attempts = [desired]
            if desired >= 8:
                attempts.append(4)
            if desired > 2:
                attempts.append(2)
            attempts.append(0)

            seen = set()
            for value in attempts:
                if value in seen:
                    continue
                seen.add(value)
                try:
                    self.render_window.SetMultiSamples(int(value))
                    applied = int(self.render_window.GetMultiSamples())
                    if value == 0:
                        final_mode = "off"
                        final_samples = 0
                        break
                    if applied >= 1:
                        final_mode = "msaa"
                        final_samples = int(value)
                        break
                except Exception:
                    continue

        if mode_norm == "off":
            self.render_window.SetMultiSamples(0)
            final_mode = "off"
            final_samples = 0

        self._antialias_mode = final_mode
        self._antialias_samples = final_samples

        if announce:
            if final_mode == "msaa":
                print(f"[VtkRobotView] Antialiasing: msaa x{final_samples}")
            else:
                print(f"[VtkRobotView] Antialiasing: {final_mode}")

        self.render_window.Render()

    def set_material_preset(self, preset: str = "soft"):
        preset_norm = (preset or "default").strip().lower()
        table = {
            "default": {"ambient": 0.20, "diffuse": 0.72, "specular": 0.22, "specular_power": 28.0},
            "soft": {"ambient": 0.32, "diffuse": 0.60, "specular": 0.08, "specular_power": 14.0},
            "studio": {"ambient": 0.26, "diffuse": 0.66, "specular": 0.14, "specular_power": 20.0},
        }
        values = table.get(preset_norm, table["default"])
        self._material_preset = preset_norm if preset_norm in table else "default"

        for actor in self._mesh_actors:
            prop = actor.GetProperty()
            prop.SetInterpolationToPhong()
            prop.SetAmbient(values["ambient"])
            prop.SetDiffuse(values["diffuse"])
            prop.SetSpecular(values["specular"])
            prop.SetSpecularPower(values["specular_power"])
        for actor in self._eye_mesh_actors:
            prop = actor.GetProperty()
            prop.SetInterpolationToPhong()
            prop.SetAmbient(values["ambient"])
            prop.SetDiffuse(values["diffuse"])
            prop.SetSpecular(values["specular"])
            prop.SetSpecularPower(values["specular_power"])
        self._update_ground_style()

        self.render_window.Render()
        if self._eye_render_window is not None:
            self._eye_render_window.Render()

    def set_background(self, theme: str = "studio"):
        theme_norm = (theme or "light").strip().lower()
        themes = {
            "white": {"bg": (1.00, 1.00, 1.00), "bg2": (1.00, 1.00, 1.00), "gradient": False, "srgb": False},
            # Keep sRGB disabled by default for compatibility; some drivers show black frame with sRGB FB.
            "light": {"bg": (0.96, 0.97, 0.98), "bg2": (0.90, 0.93, 0.97), "gradient": True, "srgb": False},
            "studio": {"bg": (0.95, 0.96, 0.98), "bg2": (0.86, 0.90, 0.96), "gradient": True, "srgb": False},
            # Pure black dark-mode background for higher contrast.
            "dark": {"bg": (0.12, 0.14, 0.18), "bg2": (0.05, 0.06, 0.08), "gradient": True, "srgb": False},
        }
        cfg = themes.get(theme_norm, themes["light"])
        self._background_theme = theme_norm if theme_norm in themes else "light"

        self.renderer.SetBackground(*cfg["bg"])
        self.renderer.SetBackground2(*cfg["bg2"])
        if cfg["gradient"]:
            self.renderer.GradientBackgroundOn()
        else:
            self.renderer.GradientBackgroundOff()

        if self._eye_renderer is not None:
            eye_bg = (0.08, 0.10, 0.14) if self._background_theme == "dark" else (0.96, 0.97, 0.98)
            self._eye_renderer.SetBackground(*eye_bg)
            self._eye_renderer.GradientBackgroundOff()

        if hasattr(self.render_window, "SetUseSRGBColorSpace"):
            try:
                self.render_window.SetUseSRGBColorSpace(bool(cfg["srgb"]))
            except Exception:
                pass
        self._update_ground_style()
        self._update_joint_label_style()

        self.render_window.Render()
        if self._eye_render_window is not None:
            self._eye_render_window.Render()

    def set_ui_theme(self, theme: str = "light"):
        theme_norm = (theme or "light").strip().lower()
        self._ui_theme = "dark" if theme_norm == "dark" else "light"
        self._update_joint_label_style()
        self.render_window.Render()

    def set_camera_preset(self, preset: str = "iso"):
        preset_norm = (preset or "iso").strip().lower()
        presets = {
            "iso": {
                "position": (0.76, -1.08, 0.66),
                "focal": (0.00, 0.00, 0.18),
                "view_up": (0.0, 0.0, 1.0),
                "view_angle": 22.0,
            },
            "front": {
                "position": (1.28, 0.00, 0.40),
                "focal": (0.00, 0.00, 0.18),
                "view_up": (0.0, 0.0, 1.0),
                "view_angle": 20.0,
            },
            "top": {
                "position": (0.00, 0.00, 1.50),
                "focal": (0.00, 0.00, 0.16),
                "view_up": (0.0, 1.0, 0.0),
                "view_angle": 18.0,
            },
        }
        cfg = presets.get(preset_norm, presets["iso"])
        self._camera_preset = preset_norm if preset_norm in presets else "iso"

        camera = self.renderer.GetActiveCamera()
        camera.SetPosition(*cfg["position"])
        camera.SetFocalPoint(*cfg["focal"])
        camera.SetViewUp(*cfg["view_up"])
        camera.SetParallelProjection(False)
        camera.SetViewAngle(float(cfg["view_angle"]))
        self.renderer.ResetCameraClippingRange()
        self.render_window.Render()

    def reset_view(self):
        self.set_camera_preset("iso")

    def reset_camera(self):
        self.reset_view()

    def set_mesh_smoothing(self, enabled: bool = True, feature_angle: float = 55.0):
        self._mesh_smoothing_enabled = bool(enabled)
        self._mesh_feature_angle = float(np.clip(feature_angle, 5.0, 175.0))

        for normals in self._normal_filters:
            normals.SetFeatureAngle(self._mesh_feature_angle)
            if self._mesh_smoothing_enabled:
                normals.SplittingOn()
            else:
                normals.SplittingOff()
            normals.Update()

        self.render_window.Render()

    def set_joint_values(self, values: Sequence[float]):
        if len(values) != len(self.joint_indices):
            raise ValueError("Joint value length mismatch")

        clipped_vals = []
        for idx, value in enumerate(values):
            lo, hi = self.joint_limits[idx]
            q = float(np.clip(value, lo, hi))
            clipped_vals.append(q)
            pb.resetJointState(
                self.pb_robot_id,
                self.joint_indices[idx],
                q,
                physicsClientId=self.pb_client,
            )

        pb.stepSimulation(physicsClientId=self.pb_client)
        self.joint_values = clipped_vals
        self._update_actor_poses()

    def reset_zero(self):
        zeros = []
        for lo, hi in self.joint_limits:
            zeros.append(float(np.clip(0.0, lo, hi)))
        self.set_joint_values(zeros)

    def _update_actor_poses(self):
        identity = _vtk_matrix_from_pose((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))

        for link_name, actors in self._actors_by_link.items():
            if link_name == self.base_link_name:
                for actor in actors:
                    actor.SetUserMatrix(identity)
                continue

            joint_idx = self._child_link_to_joint.get(link_name)
            if joint_idx is None:
                continue

            state = pb.getLinkState(
                self.pb_robot_id,
                joint_idx,
                computeForwardKinematics=True,
                physicsClientId=self.pb_client,
            )
            world_pos, world_quat = state[4], state[5]
            mat = _vtk_matrix_from_pose(world_pos, world_quat)
            for actor in actors:
                actor.SetUserMatrix(mat)

        for link_name, actors in self._eye_actors_by_link.items():
            if link_name == self.base_link_name:
                for actor in actors:
                    actor.SetUserMatrix(identity)
                continue

            joint_idx = self._child_link_to_joint.get(link_name)
            if joint_idx is None:
                continue

            state = pb.getLinkState(
                self.pb_robot_id,
                joint_idx,
                computeForwardKinematics=True,
                physicsClientId=self.pb_client,
            )
            world_pos, world_quat = state[4], state[5]
            mat = _vtk_matrix_from_pose(world_pos, world_quat)
            for actor in actors:
                actor.SetUserMatrix(mat)

        for label_joint_idx, label_actor in self._joint_label_actors:
            state = pb.getLinkState(
                self.pb_robot_id,
                label_joint_idx,
                computeForwardKinematics=True,
                physicsClientId=self.pb_client,
            )
            pos = state[4]
            label_actor.SetPosition(pos[0] + 0.03, pos[1] + 0.03, pos[2] + 0.02)

        self._update_eye_in_hand_camera_pose()

        self.render_window.Render()
        if self._eye_render_window is not None:
            self._eye_render_window.Render()

    def shutdown(self):
        if self._eye_render_window is not None:
            try:
                self._eye_render_window.Finalize()
            except Exception:
                pass
            self._eye_render_window = None
        self._eye_renderer = None
        self._eye_camera = None
        self._eye_w2i = None
        self._eye_lights.clear()

        if self.pb_client is not None:
            try:
                pb.disconnect(self.pb_client)
            except Exception:
                pass
            self.pb_client = None

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
