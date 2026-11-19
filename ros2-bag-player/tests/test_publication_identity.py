"""Fail-closed publication evidence and conservative cross-input pairing."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bag_proxy import MergeIndex
from publication_identity import identity_key


def metadata(gid="12" * 16, upper=0, scope="boot:example", domain=193, **changes):
    result = {"rig.provenance." + key: str(value) for key, value in dict(
        schema=1, kind="rmw_publication", scope=scope, rmw="rmw_zenoh_cpp",
        domain_id=domain, writer_gid=gid, sequence_upper32=upper).items()}
    result.update(changes)
    return result


class PublicationIdentityTests(unittest.TestCase):
    def test_only_explicit_supported_complete_evidence_is_accepted(self):
        good = metadata()
        self.assertIsNotNone(identity_key(good, 0))
        for key in good:
            bad = dict(good)
            del bad[key]
            with self.subTest(missing=key):
                self.assertIsNone(identity_key(bad, 1))
        invalid = [("schema", "2"), ("kind", "counter"), ("scope", ""), ("scope", "x" * 129),
                   ("scope", "a/b"), ("rmw", "rmw_fastrtps_cpp"), ("writer_gid", "00" * 16),
                   ("writer_gid", "ab" * 15), ("writer_gid", "AB" * 16), ("domain_id", "-1"),
                   ("domain_id", "01"), ("sequence_upper32", str(2**32)), ("sequence_upper32", "1.0")]
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                self.assertIsNone(identity_key({**good, "rig.provenance." + key: value}, 1))
        for seq in (-1, 2**32, True, None, "1"):
            self.assertIsNone(identity_key(good, seq))

    def test_full_width_sequence_and_unsupported_sentinel(self):
        result = json.loads(identity_key(metadata(upper=2**32-1), 2**32-2))
        self.assertEqual(result[-1], 2**64-2)
        self.assertIsNone(identity_key(metadata(upper=2**32-1), 2**32-1))
        self.assertNotEqual(identity_key(metadata(upper=1), 7), identity_key(metadata(), 7))

    def test_scope_domain_and_publisher_lifetime_are_identity_boundaries(self):
        keys = [identity_key(m, 1) for m in [metadata(), metadata(scope="other"),
                metadata(domain=194), metadata(gid="34" * 16)]]
        self.assertEqual(len(set(keys)), 4)


class PublicationMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def index(self, mode="conservative"):
        result = MergeIndex(self.root / (mode + ".db"), dict(deduplication=mode))
        self.addCleanup(result.close)
        return result

    def add(self, index, source, ordinal, sequence=1, payload=b"same", received=100,
            sent=90, evidence=None, topic="/value"):
        channel = index.channel(dict(metadata=evidence or metadata(), schema=None))
        index.add(source, ordinal, topic, "T", payload, received, sent,
                  channel=channel, sequence=sequence,
                  source_record=dict(file="part.mcap", record=ordinal, channel_id=4))

    def test_equal_publications_pair_across_inputs_without_receive_window(self):
        index = self.index()
        self.add(index, 0, 0)
        self.add(index, 1, 0, received=10**12)
        stats = index.deduplicate()
        self.assertEqual((stats["output_messages"], stats["publication_duplicates"]), (1, 1))
        index.provenance(self.root / "provenance.jsonl")
        rows = [json.loads(row) for row in (self.root / "provenance.jsonl").read_text().splitlines()]
        self.assertEqual(rows[1]["duplicate_of"], rows[0]["id"])
        self.assertEqual(rows[0]["publication_identity"][-1], 1)
        self.assertEqual(rows[0]["source_record"]["channel_id"], 4)

    def test_new_publications_and_restarted_publishers_survive_even_aggressive(self):
        index = self.index("aggressive")
        self.add(index, 0, 0, sequence=1)
        self.add(index, 1, 0, sequence=2)
        self.add(index, 2, 0, evidence=metadata(gid="34" * 16))
        self.assertEqual(index.deduplicate()["output_messages"], 3)

    def test_repeated_delivery_within_source_preserves_multiplicity(self):
        index = self.index()
        for source, count in ((0, 2), (1, 3), (2, 1)):
            for ordinal in range(count):
                self.add(index, source, ordinal, received=100 + source + ordinal)
        stats = index.deduplicate()
        self.assertEqual((stats["output_messages"], stats["publication_duplicates"]), (3, 3))

    def test_all_conflicting_identity_records_survive_including_earlier_equal_pair(self):
        for mode in ("conservative", "aggressive", "off"):
            index = self.index(mode)
            for source, data in enumerate((b"same", b"same", b"conflict")):
                self.add(index, source, 0, payload=data, received=100+source)
            stats = index.deduplicate()
            self.assertEqual(stats["output_messages"], 3)
            self.assertEqual(stats["conflicting_publication_identities"], 1)
            self.assertEqual(stats["conflicting_publication_records"], 3)
            self.assertEqual(stats["publication_duplicates"], 0)

    def test_conflicting_source_timestamp_is_not_silently_deduplicated(self):
        index = self.index()
        self.add(index, 0, 0, sent=90)
        self.add(index, 1, 0, sent=91)
        self.assertEqual(index.deduplicate()["conflicting_publication_records"], 2)

    def test_unknown_metadata_does_not_inherit_valid_neighbour_identity(self):
        index = self.index()
        self.add(index, 0, 0)
        bad = metadata(**{"rig.provenance.schema": "unknown"})
        self.add(index, 1, 0, evidence=bad)
        self.assertEqual(index.deduplicate()["output_messages"], 2)

    def test_service_event_policy_still_uses_client_request_identity(self):
        index = self.index()
        event = NS(info=NS(client_gid=b"c" * 16, sequence_number=4, event_type=1,
                           stamp=NS(sec=1, nanosec=0)))
        for source, gid in enumerate(("12"*16, "34"*16)):
            channel = index.channel(dict(metadata=metadata(gid=gid), schema=None))
            index.add(source, 0, "/control/_service_event", "std_srvs/srv/Trigger_Event",
                      b"identical-event", 100+source, 90, event, channel=channel, sequence=7)
        stats = index.deduplicate()
        self.assertEqual(stats["output_messages"], 1)
        self.assertEqual(stats["proven_duplicates"], 1)
        self.assertEqual(stats["publication_duplicates"], 0)


if __name__ == "__main__":
    unittest.main()
