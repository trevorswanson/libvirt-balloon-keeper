import pathlib
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = pathlib.Path(__file__).resolve().parents[1]
UNRAID = ROOT / "unraid"


class DistributionMetadataTests(unittest.TestCase):
    def test_community_application_metadata_is_well_formed_and_points_to_new_repo(self):
        # These are repository-controlled static metadata files, not user input.
        tree = ET.parse(UNRAID / "libvirt-balloon-keeper.xml")
        root = tree.getroot()
        plugin_url = root.findtext("PluginURL") or ""
        support = root.findtext("Support") or ""
        icon = root.findtext("Icon") or ""
        self.assertEqual(root.findtext("Plugin"), "True")
        self.assertEqual(root.findtext("Name"), "Libvirt Balloon Keeper")
        self.assertIn("trevorswanson/libvirt-balloon-keeper/main", plugin_url)
        self.assertIn("/issues", support)
        self.assertTrue(icon.endswith("libvirt-balloon-keeper.png"))

    def test_community_application_profile_is_well_formed(self):
        root = ET.parse(UNRAID / "ca_profile.xml").getroot()
        profile = root.findtext("Profile") or ""
        webpage = root.findtext("WebPage") or ""
        self.assertEqual(root.tag, "CommunityApplications")
        self.assertTrue(profile.strip())
        self.assertIn("trevorswanson/libvirt-balloon-keeper", webpage)

    def test_plugin_installer_is_thin_verified_lifecycle_wrapper(self):
        text = (UNRAID / "libvirt-balloon-keeper.plg").read_text()
        self.assertIn("releases/latest/download/libvirt-balloon-keeper.tar.gz", text)
        self.assertIn("releases/latest/download/libvirt-balloon-keeper.tar.gz.sha256", text)
        self.assertIn('sha256sum "$archive"', text)
        self.assertIn("bash /tmp/libvirt-balloon-keeper-release/unraid/lifecycle.sh install", text)
        self.assertIn("/boot/config/plugins/libvirt-balloon-keeper/lifecycle.sh uninstall", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotRegex(text, r"(password|token|secret)\\s*=", re.IGNORECASE)

    def test_release_workflow_is_tag_driven_and_publishes_stable_assets(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("tags:", text)
        self.assertIn("'v*.*.*'", text)
        self.assertIn("actions/checkout@v4", text)
        self.assertIn("gh release create", text)
        self.assertIn("libvirt-balloon-keeper.tar.gz", text)

    def test_test_workflow_builds_and_uploads_package(self):
        text = (ROOT / ".github/workflows/test.yml").read_text()
        self.assertIn("package:", text)
        self.assertIn("cmp", text)
        self.assertIn("sha256sum -c", text)
        self.assertIn("actions/upload-artifact@v4", text)


if __name__ == "__main__":
    unittest.main()
