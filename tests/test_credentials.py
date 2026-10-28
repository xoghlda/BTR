import sys
import unittest
import uuid

from trip_capture.credentials import WindowsCredentialStore


@unittest.skipUnless(sys.platform == "win32", "Windows Credential Manager")
class CredentialTests(unittest.TestCase):
    def test_write_read_delete_unicode_without_plaintext_file(self):
        account = "self-test:" + uuid.uuid4().hex
        store = WindowsCredentialStore()
        password = "test-only-한글-🔐-123"
        try:
            self.assertIsNone(store.get(account))
            store.set(account, password)
            self.assertEqual(store.get(account), password)
            store.delete(account)
            self.assertIsNone(store.get(account))
            store.delete(account)
        finally:
            store.delete(account)


if __name__ == "__main__":
    unittest.main()
