import unittest

import sync_crm_files_to_yandex as sync


class DiskPathTests(unittest.TestCase):
    def test_client_disk_url_preserves_encoded_trailing_space(self) -> None:
        url = "https://disk.yandex.ru/client/disk/root/Client%20Name%20"

        self.assertEqual(sync.normalize_disk_path(url), "root/Client Name ")

    def test_client_url_round_trip_preserves_trailing_space(self) -> None:
        path = "root/Client Name "

        self.assertEqual(
            sync.yandex_client_url(path),
            "https://disk.yandex.ru/client/disk/root/Client%20Name%20",
        )

    def test_regular_wrapping_whitespace_is_ignored(self) -> None:
        self.assertEqual(sync.normalize_disk_path("  disk:/root/Client Name  "), "root/Client Name")


if __name__ == "__main__":
    unittest.main()
