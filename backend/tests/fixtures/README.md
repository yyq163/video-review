# Native media probe regression sample

`hevc-b-frames-cfr25.mp4` is a synthetic 128×72, one-second, 25 fps
HEVC/hvc1 video with B frames. It contains no user footage. Its SPS declares
reorder depth 2. An HEVC parser without the decoder reports depth 0 in the
isolated FFmpeg build, causing the otherwise valid packet timeline to be rejected.

Generated with FFmpeg 7.1.1 and x265 build 215 (4.1+1-1d117be):

```sh
ffmpeg -v error \
  -f lavfi -i 'testsrc2=size=128x72:rate=25:duration=1' \
  -an -c:v libx265 -preset fast \
  -x265-params 'bframes=3:b-adapt=0:keyint=25:min-keyint=25:scenecut=0:pools=1:frame-threads=1:log-level=error' \
  -pix_fmt yuv420p -tag:v hvc1 hevc-b-frames-cfr25.mp4
```

The checked-in sample is 13,367 bytes, SHA-256
`a6f742091a444fe59c1b88ea548e2f66ee9f14771db01f8ffdf00cb9256d8be7`.
Different generator versions may produce different bytes with the same properties.

The Docker build checks the metadata using its freshly compiled probe. To run
the application-level regression against a specific built binary:

```sh
FCR_TEST_MEDIA_PROBE_COMMAND=/path/to/built/ffprobe \
  python -m pytest backend/tests/test_media_probe_native_runtime.py -q
```

This test never falls back to the system ffprobe. Without an explicit path it
uses `/opt/ffmpeg-apv/bin/ffprobe`, or skips if that runtime is absent.
