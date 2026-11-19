# Publication identity capture

This patch targets the official rosbag2 **0.33.4** archive, verified by SHA-256 in
the Dockerfile. The build checks the installed transport, storage, C++ writer and
MCAP/compression package versions before compiling. Only `librosbag2_transport.so`,
`librosbag2_storage_mcap.so` and `librosbag2_compression.so` are replaced; no public structure, Python binding,
CLI, or serialized CDR layout changes.

The recorder receives `rmw_message_info_t` in its existing subscription callback.
It carries publication metadata in a custom deleter on the serialized buffer's
`shared_ptr` ownership block. Queue/cache copies therefore keep the right identity
without matching by pointer address, message order, timestamps, or payload bytes.
The deleter owns the original buffer and releases it once. Its RTTI is visible
across shared libraries; the build tests that boundary with `RTLD_LOCAL`.
An immutable identity cache holds at most 128 writer/upper32 entries per topic
subscription. Eviction never invalidates queued messages.

For supported samples, MCAP channels contain these strings:

| Key | Value |
| --- | --- |
| `rig.provenance.schema` | `1` |
| `rig.provenance.kind` | `rmw_publication` |
| `rig.provenance.scope` | Explicit scope or `boot:<Linux boot UUID>` |
| `rig.provenance.rmw` | `rmw_zenoh_cpp` |
| `rig.provenance.domain_id` | Decimal domain ID |
| `rig.provenance.writer_gid` | 32 lowercase hex characters (16 complete GID bytes) |
| `rig.provenance.sequence_upper32` | Decimal upper 32 bits of publication sequence |

The MCAP Message `sequence` field holds the lower 32 bits. Sequence zero is valid;
`UINT64_MAX` means unsupported and is never marked. `publishTime` remains the
original ROS source timestamp, and `logTime` retains the recorder's existing
receive/simulation-time behavior. `topic_type_hash` uses the existing MCAP key.
Writer and upper32 changes create separate channels; unsupported samples use an
unmarked channel and cannot inherit the preceding sample's identity.
Both ordinary topic channels and publication channels count toward MCAP's 65,535
channel limit per file. The writer stops with a `split the bag` error before a
further allocation can wrap its channel ID and associate data with an old writer.

The Rig logger sets `RIG_PUBLICATION_SCOPE` at recording startup from the pinned run's
`manifest.yaml` field `run_uuid`. All hosts sharing that run UUID use the same scope.
An explicit override takes precedence and accepts 1–128 ASCII letters, digits, underscores,
hyphens, periods or colons. The recorder uses its Linux boot ID if the variable is empty,
including standalone recording and older runs without a UUID. An invalid explicit
value, missing boot ID, zero GID, unsupported sequence, intra-process origin, or
unqualified RMW produces an ordinary unmarked record.
Cross-process identity has been validated with `rmw_zenoh_cpp` 0.10.6. Other RMWs
remain unmarked; future fleet builds must pass the two-recorder runtime test.

Every file containing marked records also receives an uncompressed MCAP Metadata
record named `rig.publication_provenance`, with `schema=1` and
`contract=rmw_publication`. It signals presence, not validity of individual data.
The MCAP reader preserves the original channel metadata and sequence in the same
ownership envelope, so normal CDR MCAP conversion, filtering, chunk compression
and splitting retain the evidence. The compression reader/writer also carries
identity, sequence and both timestamps across **MESSAGE zstd** buffer replacement;
**FILE zstd** wraps the complete MCAP and preserves its channels unchanged. Other
serialization conversions may replace the buffer and are ineligible for assumed
preservation. Export tools verify retained records before
publishing output.

The installed capability is
`/opt/ros/$ROS_DISTRO/share/rig_publication_provenance/capability.json`.
`test_envelope.cpp` runs in the compiler stage. The runtime build gate executes
`../test_publication_provenance.py` against actual `ros2 bag convert`, exercising
multiple writers, upper32 wrap, near-maximum sequence, unmarked records, original
payloads/timestamps/metadata, chunk recompression, MESSAGE/FILE zstd roundtrips,
splits and per-file presence markers.
Ordinary playback remains standard ROS CDR; no new message subscriptions are used.
