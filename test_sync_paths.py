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


class CrmFieldValueTests(unittest.TestCase):
    def test_lead_folder_value_preserves_trailing_space(self) -> None:
        lead = {"custom_fields_values": [{"field_id": 7, "values": [{"value": "root/Client "}]}]}

        self.assertEqual(sync.lead_folder_value(lead, 7), "root/Client ")

    def test_event_folder_value_preserves_trailing_space(self) -> None:
        event = {"value_after": [{"custom_field_value": {"field_id": 7, "text": "root/Client "}}]}

        self.assertEqual(sync.event_folder_value(event, 7), "root/Client ")

    def test_blank_crm_field_value_is_empty(self) -> None:
        self.assertEqual(sync.crm_field_text("   "), "")


class FakeDisk(sync.YandexDiskClient):
    def __init__(self, existing: set[str]) -> None:
        self.existing = set(existing)
        self.created: list[str] = []

    def exists(self, path: str) -> bool:
        return sync.normalize_disk_path(path) in self.existing

    def ensure_single_folder(self, folder_path: str) -> None:
        path = sync.normalize_disk_path(folder_path)
        if path not in self.existing:
            self.created.append(path)
            self.existing.add(path)


class DiskFolderCreationTests(unittest.TestCase):
    def test_creates_only_crm_files_folder_inside_existing_client_folder(self) -> None:
        root = sync.configured_disk_root()
        subfolder = sync.crm_files_subfolder_name()
        disk = FakeDisk({f"{root}/Client"})

        disk.ensure_crm_files_folder(f"{root}/Client/{subfolder}")

        self.assertEqual(disk.created, [f"{root}/Client/{subfolder}"])
        self.assertNotIn(f"{root}/Missing", disk.created)

    def test_does_not_create_missing_client_folder(self) -> None:
        root = sync.configured_disk_root()
        subfolder = sync.crm_files_subfolder_name()
        disk = FakeDisk({root})

        with self.assertRaises(sync.MissingClientFolderError):
            disk.ensure_crm_files_folder(f"{root}/Missing Client/{subfolder}")

        self.assertEqual(disk.created, [])

    def test_root_is_not_treated_as_client_folder(self) -> None:
        root = sync.configured_disk_root()
        subfolder = sync.crm_files_subfolder_name()
        disk = FakeDisk({root})

        with self.assertRaises(sync.MissingClientFolderError):
            disk.ensure_crm_files_folder(f"{root}/{subfolder}")

        self.assertEqual(disk.created, [])


if __name__ == "__main__":
    unittest.main()
