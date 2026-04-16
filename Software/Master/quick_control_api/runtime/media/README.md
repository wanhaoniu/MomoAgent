Put the HaiGuiTang intro video here as:

`haiguitang_intro.mp4`

If that file exists, the scene config endpoint can automatically expose it through:

`/api/v1/scenes/haiguitang/intro-video`

You can also override the intro video by editing:

`../haiguitang_scene.json`

and setting `intro_video_url` to any reachable `http://`, `https://`, or relative API URL.
