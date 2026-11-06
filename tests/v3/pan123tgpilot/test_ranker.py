import sys
from pathlib import Path
import unittest

plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123tgpilot.ranker import calculate_quality_score, evaluate_upgrade


class TestRanker(unittest.TestCase):
    def test_calculate_quality_score_weights(self):
        score_4k = calculate_quality_score(
            resolution="2160p",
            dovi_hdr="DoVi",
            codec="H.265",
            source="BluRay",
            file_size_bytes=40 * (1024 ** 3),
        )
        score_1080p = calculate_quality_score(
            resolution="1080p",
            dovi_hdr="",
            codec="H.264",
            source="WEB-DL",
            file_size_bytes=8 * (1024 ** 3),
        )
        score_720p = calculate_quality_score(
            resolution="720p",
            dovi_hdr="",
            codec="H.264",
            source="HDTV",
            file_size_bytes=2 * (1024 ** 3),
        )

        self.assertGreater(score_4k, score_1080p)
        self.assertGreater(score_1080p, score_720p)

    def test_evaluate_upgrade(self):
        new_resource = {
            "resolution": "2160p",
            "codec": "H.265",
            "dovi_hdr": "DoVi",
            "source": "WEB-DL",
            "quality_score": 150,
        }
        old_record = {
            "resolution": "1080p",
            "codec": "H.264",
            "dovi_hdr": "",
            "quality_score": 75,
        }
        should_upgrade, reason = evaluate_upgrade(old_record, new_resource)
        self.assertTrue(should_upgrade)
        self.assertIn("触发洗版升级", reason)

        # 若旧版得分已达到 180 分 (更高)，则不应升级
        old_record_high = {
            "resolution": "2160p",
            "codec": "H.265",
            "dovi_hdr": "DoVi",
            "quality_score": 180,
        }
        should_upgrade_fail, reason_fail = evaluate_upgrade(old_record_high, new_resource)
        self.assertFalse(should_upgrade_fail)
        self.assertIn("跳过转存", reason_fail)


if __name__ == "__main__":
    unittest.main()
