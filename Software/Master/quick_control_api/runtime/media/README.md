Recommended HaiGuiTang media filenames:

`begin.mp4`
`default.mp4`
`nod.mp4`
`shake.mp4`
`end.mp4`

Put these files in:

`Software/Master/quick_control_api/runtime/media/`

The scene config endpoint will automatically detect these files and expose them to the app. If
`default.mp4` is missing, the app will temporarily fall back to `begin.mp4` as the default full-screen
character loop.

The intro file can also still be named:

`haiguitang_intro.mp4`

If an intro file exists, it can be accessed through:

`/api/v1/scenes/haiguitang/intro-video`

Specific clips can also be accessed through:

`/api/v1/scenes/haiguitang/media/intro`
`/api/v1/scenes/haiguitang/media/default`
`/api/v1/scenes/haiguitang/media/nod`
`/api/v1/scenes/haiguitang/media/shake`
`/api/v1/scenes/haiguitang/media/outro`

The app also exposes a scene-state POST interface so an agent can switch the full-screen clip and the
floating subtitle:

`POST /api/v1/scenes/haiguitang/state`

Example body:

`{"clip":"nod","subtitle_text":"这次是肯定回答。","loop_playback":false}`

You can also override the intro video by editing:

`../haiguitang_scene.json`

and setting `intro_video_url`, `default_video_url`, `nod_video_url`, `shake_video_url`, or `outro_video_url`
to any reachable `http://`, `https://`, or relative API URL.
