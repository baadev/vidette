"""`vidette collect`: build a labelled-clip dataset from a camera that wakes on motion.

A dataset tool, not a recorder (ADR-0010): it waits for a battery camera's RTSP stream
to appear, copies each wake period to `clips/<YYYYMMDD-HHMMSS>.mp4` with a few key frames
and a JSON sidecar, and stops at hard disk limits. It never runs inside `vidette serve`.
"""
