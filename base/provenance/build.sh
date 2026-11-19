#!/bin/bash
# Build just the three private implementations against the installed, unchanged ROS ABI.
set -eo pipefail
source "/opt/ros/${ROS_DISTRO}/setup.bash"
set -u
source_dir="${1:?rosbag2 source directory required}"
patch_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 - <<'PY'
import os
from pathlib import Path
from xml.etree import ElementTree
for package in ('rosbag2_transport', 'rosbag2_storage', 'rosbag2_cpp', 'rosbag2_storage_mcap',
                'rosbag2_compression'):
    path = Path('/opt/ros') / os.environ['ROS_DISTRO'] / 'share' / package / 'package.xml'
    version = ElementTree.parse(path).findtext('version')
    if version != '0.33.4':
        raise SystemExit(f'Publication provenance requires rosbag2 0.33.4; {package} is {version}. '
                         'Update and validate the pinned patch before changing the base version.')
PY
patch --directory "$source_dir" --strip 1 --fuzz 0 < "$patch_dir/rosbag2-0.33.4.patch"
cp "$patch_dir/rig_publication_provenance.hpp" \
   "$source_dir/rosbag2_transport/src/rosbag2_transport/rig_publication_provenance.hpp"
cp "$patch_dir/rig_publication_provenance.hpp" \
   "$source_dir/rosbag2_storage_mcap/src/rig_publication_provenance.hpp"
cp "$patch_dir/rig_publication_provenance.hpp" \
   "$source_dir/rosbag2_compression/src/rosbag2_compression/rig_publication_provenance.hpp"
g++ -std=c++17 -O2 -fvisibility=hidden -fPIC -shared -DRIG_PRODUCER \
  -I"/opt/ros/$ROS_DISTRO/include/rcutils" "$patch_dir/test_envelope.cpp" -o /tmp/rig-envelope.so
g++ -std=c++17 -O2 -fvisibility=hidden -I"/opt/ros/$ROS_DISTRO/include/rcutils" \
  "$patch_dir/test_envelope.cpp" -ldl -o /tmp/rig-envelope-test
/tmp/rig-envelope-test /tmp/rig-envelope.so
mkdir -p /rig-provenance/lib /rig-provenance/share/rig_publication_provenance
for package in rosbag2_transport rosbag2_storage_mcap rosbag2_compression; do
  cmake -S "$source_dir/$package" -B "/tmp/build-$package" \
    -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release
  cmake --build "/tmp/build-$package" --target "$package" --parallel "${RIG_BUILD_JOBS:-2}"
  cp "/tmp/build-$package/lib$package.so" /rig-provenance/lib/
done
cp "$patch_dir/capability.json" /rig-provenance/share/rig_publication_provenance/
