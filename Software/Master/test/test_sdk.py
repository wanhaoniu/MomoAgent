from soarmmoce_sdk import Robot

def main():
    robot = Robot()  # 默认配置
    try:

        robot.connect()
        print("transport:", type(robot._transport).__name__)

        state = robot.get_state()
        print("connected:", state.connected)
        print("q:", state.joint_state.q)
        print("tcp:", state.tcp_pose.xyz, state.tcp_pose.rpy)    
        # 关节运动
        q = state.joint_state.q.copy()
        robot.move_joints(q, duration=1.0, wait=True, timeout=2.0)

        # 笛卡尔运动（rpy=None 表示保持当前姿态）
        p = robot.get_state().tcp_pose
        robot.move_tcp(
            x=float(p.xyz[0]+0.1),
            y=float(p.xyz[1]),
            z=float(p.xyz[2]),
            rpy=None,
            frame="base",
            duration=0.5,
            wait=True,
            timeout=2.0,
        )
        state = robot.get_state()
        print("tcp:", state.tcp_pose.xyz, state.tcp_pose.rpy) 
        # 夹爪
        robot.set_gripper(open_ratio=0.5, wait=True, timeout=2.0)

        # 回 home
        robot.home(duration=1.0, wait=True, timeout=2.0)

        # 急停接口
        robot.stop()
    finally:
        robot.disconnect()

if __name__ == "__main__":
    main()
