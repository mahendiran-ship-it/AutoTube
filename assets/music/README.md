# Music library

AutoTube AI never ships or downloads copyrighted music. To add background music:

1. Download tracks you are licensed to use (see `library.example.yaml` for suggested free sources).
2. Put the files in this folder.
3. Copy `library.example.yaml` to `library.yaml` and describe each track, **including its license**.

Tracks without license information are ignored. If the library is empty the video is still produced using
the project's own generated ambient bed and sound effects (original, no third-party rights).
Tracks with `attribution_required: true` automatically get a credit line in the video description.
