#!/usr/bin/env python3
"""Run inside fleet-ros or fleet-ros-msgs; exercise the shipped ROS/bag runtime."""
import importlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

import rclpy
from rclpy.serialization import deserialize_message, serialize_message
import rosbag2_py
from rosidl_runtime_py.utilities import get_message
import yaml


class RuntimeSmoke(unittest.TestCase):
    def test_tools_and_runtime_interfaces(self):
        # ROS Debian runtime packages retain some headers and CMake dependencies.
        for tool in ('gcc', 'g++', 'colcon', 'rosdep'):
            self.assertIsNone(shutil.which(tool), f'{tool} leaked into runtime')
        for module in ('rclpy', 'rosbag2_py', 'yaml', 'sensor_msgs.msg', 'nav_msgs.msg',
                       'tf2_msgs.msg', 'diagnostic_msgs.msg', 'rosbag2_interfaces.srv'):
            importlib.import_module(module)
        for command in (('bag', 'record'), ('bag', 'play'), ('bag', 'convert'),
                        ('topic', 'list'), ('lifecycle', 'get'), ('service', 'call')):
            subprocess.run(['ros2', *command, '--help'], check=True, stdout=subprocess.DEVNULL, timeout=30)

    def test_record_convert_read_and_replay(self):
        kind = os.environ.get('FLEET_TEST_MESSAGE', 'std_msgs/msg/String')
        cls = get_message(kind)
        message = cls()
        if kind == 'std_msgs/msg/String':
            message.data = 'runtime-smoke-' * 1000
        payload = serialize_message(message)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, converted = str(root/'recorded'), str(root/'exported')
            writer = rosbag2_py.SequentialWriter()
            writer.open(rosbag2_py.StorageOptions(uri=source, storage_id='mcap'),
                        rosbag2_py.ConverterOptions('', ''))
            writer.create_topic(rosbag2_py.TopicMetadata(id=0, name='/runtime_smoke', type=kind,
                                                        serialization_format='cdr'))
            for i in range(10):
                writer.write('/runtime_smoke', payload, 1_000_000_000 + i * 10_000_000)
            writer.close()
            options = root/'convert.yaml'
            options.write_text(yaml.safe_dump({'output_bags': [{'uri': converted, 'storage_id': 'mcap',
                                  'all_topics': True, 'storage_preset_profile': 'zstd_small'}]}))
            subprocess.run(['ros2', 'bag', 'convert', '-i', source, '-o', str(options)], check=True, timeout=60)
            subprocess.run(['ros2', 'bag', 'info', converted], check=True, stdout=subprocess.DEVNULL, timeout=30)
            reader = rosbag2_py.SequentialReader()
            reader.open(rosbag2_py.StorageOptions(uri=converted, storage_id='mcap'),
                        rosbag2_py.ConverterOptions('', ''))
            count = 0
            while reader.has_next():
                topic, data, stamp = reader.read_next()
                self.assertEqual(topic, '/runtime_smoke')
                self.assertEqual(deserialize_message(data, cls), message)
                count += 1
            self.assertEqual(count, 10)
            router = None
            player = None
            recorder = None
            node = None
            try:
                if os.environ.get('RMW_IMPLEMENTATION') == 'rmw_zenoh_cpp':
                    router = subprocess.Popen(['ros2', 'run', 'rmw_zenoh_cpp', 'rmw_zenohd'],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1)
                    self.assertIsNone(router.poll(), 'Zenoh router exited')
                rclpy.init()
                node = rclpy.create_node('runtime_smoke_listener')
                received = []
                sub = node.create_subscription(cls, '/runtime_smoke', received.append, 20)
                recorded = str(root/'recorded-from-replay')
                with open(root/'record.log', 'w+') as record_log, open(root/'play.log', 'w+') as log:
                    recorder = subprocess.Popen(['ros2', 'bag', 'record', '-s', 'mcap', '-o', recorded,
                                                 '--disable-keyboard-controls', '--topics', '/runtime_smoke'],
                                                stdout=record_log, stderr=subprocess.STDOUT)
                    player = subprocess.Popen(['ros2', 'bag', 'play', converted, '--disable-keyboard-controls',
                                               '--delay', '2', '--wait-for-all-acked', '2'],
                                              stdout=log, stderr=subprocess.STDOUT)
                    deadline = time.monotonic() + 30
                    while len(received) < 10 and time.monotonic() < deadline:
                        rclpy.spin_once(node, timeout_sec=0.1)
                        if player.poll() is not None and player.returncode != 0:
                            break
                    log.seek(0)
                    self.assertEqual(len(received), 10, log.read())
                    self.assertTrue(all(item == message for item in received))
                    self.assertEqual(player.wait(timeout=15), 0)
                    recorder.send_signal(signal.SIGINT)
                    record_code = recorder.wait(timeout=15)
                    record_log.seek(0)
                    self.assertEqual(record_code, 0, record_log.read())
                    record_log.seek(0)
                    info = rosbag2_py.Info().read_metadata(recorded, 'mcap')
                    self.assertEqual(info.message_count, 10, record_log.read())
            finally:
                for proc in (recorder, player, router):
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                if node is not None:
                    node.destroy_node()
                if rclpy.ok():
                    rclpy.shutdown()


if __name__ == '__main__':
    unittest.main(verbosity=2)
