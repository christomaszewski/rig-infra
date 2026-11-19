"""Two real Zenoh recorders, a publisher restart, a slim export and a conservative merge.

Run in the fleet ROS image in an isolated network namespace (docker --network none).
Pass an empty output directory to retain the source bags, export, proxy and report.
"""
import argparse
from contextlib import ExitStack
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from mcap.reader import make_reader
import yaml

ROOT = Path(__file__).resolve().parents[1]
TOPIC = '/rig_identity_test'
PER_PUBLISHER = 12


def publish():
    import rclpy
    from std_msgs.msg import String

    rclpy.init()
    node = rclpy.create_node('test_publisher')
    try:
        publisher = node.create_publisher(String, TOPIC, 50)
        deadline = time.monotonic() + 20
        while publisher.get_subscription_count() < 2:
            if time.monotonic() >= deadline:
                raise RuntimeError('Both recorder subscriptions must be present')
            time.sleep(0.05)
        # Every publication has identical CDR, across sequences AND publisher restarts.
        for _ in range(PER_PUBLISHER):
            publisher.publish(String(data='same payload'))
            time.sleep(0.025)
        time.sleep(0.4)
    finally:
        node.destroy_node()
        rclpy.shutdown()


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise


def publications(bag, expected_scope):
    observations = []
    for split in sorted(bag.glob('*.mcap')):
        with split.open('rb') as stream:
            for _, channel, message in make_reader(stream).iter_messages(topics=[TOPIC]):
                fields = channel.metadata
                scope = fields['rig.provenance.scope']
                gid = fields['rig.provenance.writer_gid']
                assert fields['rig.provenance.schema'] == '1'
                assert fields['rig.provenance.kind'] == 'rmw_publication'
                assert fields['rig.provenance.domain_id'] == '193'
                assert fields['rig.provenance.rmw'] == 'rmw_zenoh_cpp'
                assert scope == expected_scope, (scope, expected_scope)
                assert re.fullmatch('[0-9a-f]{32}', gid) and int(gid, 16)
                sequence = int(fields['rig.provenance.sequence_upper32']) << 32 | message.sequence
                observations.append((gid, sequence, message.publish_time, message.data))
    return observations


def check(root):
    os.environ.update(RMW_IMPLEMENTATION='rmw_zenoh_cpp', ROS_DOMAIN_ID='193',
                      ZENOH_CONFIG_OVERRIDE='scouting/multicast/enabled=false')
    for key in ('RIG_PUBLICATION_SCOPE', 'RIG_SIM_TIME'):
        os.environ.pop(key, None)
    logger = ROOT / 'ros2-bag-logger'
    run_uuid = str(uuid.uuid4())
    run_dirs = []
    with ExitStack() as children:
        def start(name, command, env=None):
            log = children.enter_context((root / (name + '.log')).open('w'))
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            children.callback(stop, process)
            return process

        router = start('router', ['ros2', 'run', 'rmw_zenoh_cpp', 'rmw_zenohd'])
        time.sleep(1)
        assert router.poll() is None
        recorders = []
        for name in ('logger_a', 'logger_b'):
            # Independent registry roots and local labels, as on two recording hosts.
            data_root = root / name
            run = data_root / 'runs' / (name + '_survey')
            run.mkdir(parents=True)
            (run / 'manifest.yaml').write_text(yaml.safe_dump({'run': run.name, 'run_uuid': run_uuid}))
            (data_root / 'current').symlink_to(run.relative_to(data_root))
            run_dirs.append(run)
            cfg = root / (name + '.yaml')
            cfg.write_text(yaml.safe_dump({'name': name, 'record': {'mode': 'allow', 'topics': [TOPIC]}}))
            rendered = subprocess.check_output([sys.executable, logger / 'tools/bag_cmd.py', cfg, root],
                                               text=True).strip().split('\t')
            recorders.append(start(name, [rendered[1]], env={**os.environ, 'RIG_BAG_ROOT': str(data_root)}))
        for _ in range(2):
            subprocess.run([sys.executable, __file__, '--publish'], check=True, timeout=30)
        for process in recorders:
            stop(process)
            assert process.returncode == 0, process.returncode

    verify_recordings(root, run_dirs, run_uuid)


def verify_recordings(root, run_dirs=None, expected_scope=None):
    # Direct, standalone logger launches have no run registry.
    run_a, run_b = (root, root) if run_dirs is None else run_dirs
    if expected_scope is None:
        expected_scope = 'boot:' + Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    logger = ROOT / 'ros2-bag-logger'
    source_a, = (run_a / 'bags/logger_a').iterdir()
    source_b, = (run_b / 'bags/logger_b').iterdir()
    expected = publications(source_a, expected_scope)
    second = publications(source_b, expected_scope)
    assert len(expected) == 2 * PER_PUBLISHER, len(expected)
    assert sorted(expected) == sorted(second), 'The recorders disagree on publication identity or content'
    # Two publisher lifetimes, each with exactly 12 distinct publications.
    publishers = {gid: sorted(seq for writer, seq, *_ in expected if writer == gid)
                  for gid, *_ in expected}
    assert len(publishers) == 2, publishers
    assert all(values == list(range(1, PER_PUBLISHER + 1)) for values in publishers.values()), publishers
    assert len({payload for *_, payload in expected}) == 1

    exported = root / 'export'
    exported.mkdir()
    rendered = subprocess.check_output([sys.executable, logger / 'tools/bag_export.py',
                                        root / 'logger_b.yaml', logger], text=True,
                                       env={**os.environ, 'RIG_EXPORT_SOURCE': str(run_b),
                                            'RIG_EXPORT_DEST': str(exported)}).strip().split('\t')
    subprocess.run(['bash', rendered[0]], check=True)
    exported_b = exported / 'bags/logger_b' / source_b.name
    assert sorted(publications(exported_b, expected_scope)) == sorted(expected)

    dest = root / 'proxy'
    dest.mkdir()
    request = dict(version=1, destination=str(dest), result=str(dest/'result.json'),
                   policy={'deduplication': 'conservative', 'time_source': 'received'},
                   bags=[dict(storage='mcap', logger=p.parent.name, session=p.name, path=str(p))
                         for p in (source_a, exported_b)])
    config = root / 'merge.json'
    config.write_text(json.dumps(request))
    subprocess.run([sys.executable, ROOT / 'ros2-bag-player/tools/bag_proxy.py', config], check=True)
    stats = json.loads((dest / 'result.json').read_text())['statistics']
    assert stats['publication_duplicates'] == len(expected), stats
    assert stats['proven_duplicates'] == len(expected), stats
    assert stats['output_messages'] == len(expected), stats
    assert sorted(publications(dest / 'bag', expected_scope)) == sorted(expected)
    report = dict(publication_scope=expected_scope, recorded_per_logger=len(expected), distinct_publisher_channels=len(publishers), sequences_by_gid=publishers,
                  distinct_payloads=1, exported=len(expected), merge=stats)
    (root / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.publish:
        publish()
    elif args.output:
        args.output.mkdir(exist_ok=True)
        if any(args.output.iterdir()):
            parser.error('--output requires an empty directory')
        check(args.output.resolve())
    else:
        with tempfile.TemporaryDirectory() as temporary:
            check(Path(temporary))
