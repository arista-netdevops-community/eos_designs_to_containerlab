"""Regression tests for link discovery and version-dependent topology output."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ansible.plugins.action import ActionBase
from ansible.errors import AnsibleError
import yaml


ROLE_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("simulation", ROLE_DIR / "action_plugins/simulation.py")
SIMULATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SIMULATION)


class SimulationLinksTests(unittest.TestCase):
    def test_interface_names_are_normalized_regardless_of_case(self):
        action = SIMULATION.ActionModule.__new__(SIMULATION.ActionModule)
        for name, expected in (
            ("Ethernet1", "eth1"),
            ("ethernet1", "eth1"),
            ("ETHERNET1", "eth1"),
            ("Ethernet49/1.1111", "eth49_1"),
            ("ethernet49/1.1111", "eth49_1"),
            ("eth1", "eth1"),
            ("ens3", "ens3"),
        ):
            with self.subTest(name=name):
                self.assertEqual(action._eos_intf_to_clab(name), expected)

    def render_topology(self, directory, mapping, same_number=True, parents=True, one_container=False, external_links=None,
                        copy_to_flash=False, version=None, detected_version=None):
        directory = Path(directory)
        hostvars = {
            "CL_1": {
                "inventory_dir": str(directory),
                "sim_dir": str(directory) + "/",
                "sim_include_avd_external_nodes": True,
                "containerlab_deploy_local": True,
                "containerlab_custom_interface_mapping": mapping,
                "containerlab_custom_interface_mapping_same_number": same_number,
                "sim_external_node_one_container": one_container,
                "containerlab_external_links": external_links if external_links is not None else [],
                "containerlab_ceos_copy_to_flash": copy_to_flash,
                "containerlab_version": version,
                "containerlab_detected_version": detected_version,
            },
            "old-core1": {},
            "old-core2": {},
        }
        for leaf, core_port in (("leaf1a", "Ethernet1"), ("leaf1b", "Ethernet2")):
            interfaces = []
            for core, port in (("old-core1", "Ethernet47"), ("old-core2", "Ethernet48")):
                for suffix in (("", ".1111", ".2222") if parents else (".1111", ".2222")):
                    interfaces.append({
                        "name": port + suffix,
                        "metadata": {
                            "peer": core,
                            "peer_interface": core_port + suffix,
                            "peer_type": "other",
                        },
                    })
            # Both ends of an internal link still need an interface mapping.
            interfaces.append({
                "name": "Ethernet49/1",
                "metadata": {
                    "peer": "leaf1b" if leaf == "leaf1a" else "leaf1a",
                    "peer_interface": "Ethernet49/1",
                    "peer_type": "l3leaf",
                },
            })
            hostvars[leaf] = {"ethernet_interfaces": interfaces}

        action = SIMULATION.ActionModule.__new__(SIMULATION.ActionModule)
        with patch.object(ActionBase, "run", return_value={}):
            action.run(task_vars={
                "ansible_play_hosts_all": ["leaf1a", "leaf1b"],
                "hostvars": hostvars,
                "groups": {"SIMULATION": ["CL_1"]},
            })
        return yaml.safe_load((directory / "CL_1_topology.clab.yml").read_text())["topology"]

    def test_external_ports_and_subinterfaces(self):
        for mapping, same_number in ((False, True), (True, True), (True, False)):
            with self.subTest(mapping=mapping, same_number=same_number), tempfile.TemporaryDirectory() as directory:
                topology = self.render_topology(directory, mapping, same_number)
                endpoints = [endpoint for link in topology["links"] for endpoint in link["endpoints"]]
                self.assertEqual(len(topology["links"]), 5)
                self.assertEqual(len(endpoints), len(set(endpoints)))
                core_endpoints = {endpoint for endpoint in endpoints if endpoint.startswith("old-core")}
                self.assertEqual(core_endpoints, {
                    "old-core1:eth1", "old-core1:eth2", "old-core2:eth1", "old-core2:eth2",
                })
                self.assertTrue(all("." not in endpoint for endpoint in endpoints))
                if mapping:
                    for leaf in ("leaf1a", "leaf1b"):
                        data = json.loads((Path(directory) / f"CL_1_mappings/{leaf}.json").read_text())
                        self.assertEqual(set(data["EthernetIntf"].values()), {"Ethernet47", "Ethernet48", "Ethernet49/1"})
                        expected_ports = {"eth47", "eth48", "eth49_1"} if same_number else {"eth1", "eth2", "eth3"}
                        self.assertEqual(set(data["EthernetIntf"]), expected_ports)
                        self.assertTrue({f"{leaf}:{port}" for port in expected_ports}.issubset(endpoints))

    def test_subinterfaces_without_explicit_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            topology = self.render_topology(directory, mapping=True, parents=False)
            self.assertEqual(len(topology["links"]), 5)
            endpoints = [endpoint for link in topology["links"] for endpoint in link["endpoints"]]
            self.assertEqual(len(endpoints), len(set(endpoints)))

    def test_single_external_container_has_one_port_per_cable(self):
        with tempfile.TemporaryDirectory() as directory:
            topology = self.render_topology(directory, mapping=True, one_container=True)
            self.assertEqual(len(topology["links"]), 5)
            client_ports = {
                endpoint for link in topology["links"] for endpoint in link["endpoints"]
                if endpoint.startswith("client_all:")
            }
            self.assertEqual(client_ports, {f"client_all:eth{number}" for number in range(1, 5)})

    def test_external_to_external_link_is_rendered_once(self):
        links = [
            {"endpoints": ["old-core1:eth5", "old-core2:eth5"]},
            {"endpoints": ["old-core2:eth5", "old-core1:eth5"]},
        ]
        for mapping, same_number in ((False, True), (True, True), (True, False)):
            with self.subTest(mapping=mapping, same_number=same_number), tempfile.TemporaryDirectory() as directory:
                topology = self.render_topology(directory, mapping, same_number, external_links=links)
                self.assertEqual(len(topology["links"]), 6)
                self.assertIn(links[0], topology["links"])
                endpoints = [endpoint for link in topology["links"] for endpoint in link["endpoints"]]
                self.assertEqual(len(endpoints), len(set(endpoints)))
                self.assertTrue({"old-core1", "old-core2"}.issubset(topology["nodes"]))

    def test_invalid_external_links_fail_with_clear_errors(self):
        for links, message in (
            ({"endpoints": []}, "must be a list"),
            ([{"endpoints": ["old-core1:eth5"]}], "needs two endpoints"),
            ([{"endpoints": ["old-core1", "old-core2:eth5"]}], "node:interface"),
            ([{"endpoints": ["unknown:eth5", "old-core2:eth5"]}], "not a discovered external node"),
            ([{"endpoints": ["old-core1:Ethernet1/1", "old-core2:eth5"]}], "physical containerlab interface"),
            ([{"endpoints": ["old-core1:eth5.111", "old-core2:eth5"]}], "physical containerlab interface"),
            ([{"endpoints": ["old-core1:eth5", "old-core1:eth6"]}], "different nodes"),
            ([{"endpoints": ["old-core1:eth1", "old-core2:eth5"]}], "already in use"),
            ([
                {"endpoints": ["old-core1:eth5", "old-core2:eth5"]},
                {"endpoints": ["old-core1:eth5", "old-core2:eth6"]},
            ], "already in use"),
        ):
            with self.subTest(links=links), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(AnsibleError, message):
                    self.render_topology(directory, mapping=True, external_links=links)

    def test_external_links_require_individual_external_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AnsibleError, "sim_external_node_one_container: false"):
                self.render_topology(directory, mapping=True, one_container=True, external_links=[
                    {"endpoints": ["old-core1:eth5", "old-core2:eth5"]},
                ])

    def test_topology_uses_the_deployment_hosts_version(self):
        for version, detected_version, modern in (
            ("0.80.0", None, False),
            (None, "0.81.0", True),
            ("0.80.0", "0.81.0", False),
            ("0.100.0", None, True),
        ):
            with self.subTest(version=version, detected=detected_version), tempfile.TemporaryDirectory() as directory:
                topology = self.render_topology(directory, mapping=True, copy_to_flash=True,
                                                version=version, detected_version=detected_version)
                for leaf in ("leaf1a", "leaf1b"):
                    node = topology["nodes"][leaf]
                    if modern:
                        self.assertNotIn("extras", node)
                        paths = node["copy-to-flash"]
                    else:
                        self.assertNotIn("copy-to-flash", node)
                        paths = node["extras"]["ceos-copy-to-flash"]
                    self.assertEqual(paths, [f"CL_1_mappings/{leaf}/EosIntfMapping.json"])

    def test_missing_or_invalid_versions_fail_before_writing_topology(self):
        for version in (None, "latest", "not-a-version"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(AnsibleError, "containerlab_version"):
                    self.render_topology(directory, mapping=True, copy_to_flash=True, version=version)
                self.assertFalse((Path(directory) / "CL_1_topology.clab.yml").exists())


class CopyToFlashTests(unittest.TestCase):
    def render_nodes(self, directory, versions, copy=True, explicit_token_name=True):
        action = SIMULATION.ActionModule.__new__(SIMULATION.ActionModule)
        nodes = {}
        hostvars = {}
        for index, sim_host in enumerate(versions):
            node = f"ceos{index}"
            nodes[sim_host] = {node: {"kind": "arista_ceos", "image": "ceos:latest"}}
            hostvars[node] = {
                "metadata": {"serial_number": "TESTSERIAL", "platform": "cEOS"},
                "daemon_terminattr": {"cvauth": {"token_file": "/mnt/flash/onboarding-token"}},
                "containerlab": {"bind": "local-data:/data:ro"},
            }
        result = action.create_clab_topology_nodes(
            nodes, hostvars, True, 20, True, "test-token", True, True,
            list(hostvars), False, "", "onboarding-token" if explicit_token_name else None,
            copy, str(directory) + "/", containerlab_versions=versions,
        )
        return {host: yaml.safe_load(content) for host, content in result.items()}

    def test_all_flash_artifacts_and_custom_binds_work_on_mixed_versions(self):
        for explicit_token_name in (True, False):
            with self.subTest(explicit_token_name=explicit_token_name), tempfile.TemporaryDirectory() as directory:
                result = self.render_nodes(directory, {"old": "0.80.2", "new": "v0.81.1"},
                                           explicit_token_name=explicit_token_name)
                for index, sim_host in enumerate(("old", "new")):
                    node_name = f"ceos{index}"
                    node = result[sim_host][node_name]
                    if sim_host == "old":
                        self.assertNotIn("copy-to-flash", node)
                        paths = node["extras"]["ceos-copy-to-flash"]
                    else:
                        self.assertNotIn("extras", node)
                        paths = node["copy-to-flash"]
                    prefix = f"{sim_host}_mappings/{node_name}"
                    self.assertEqual(paths, [
                        f"{prefix}/EosIntfMapping.json",
                        f"{prefix}/onboarding-token",
                        f"{prefix}/ceos-config",
                        f"{prefix}/{node_name}-platform",
                    ])
                    self.assertEqual(node["binds"], ["local-data:/data:ro"])
                    self.assertEqual((Path(directory) / prefix / "onboarding-token").read_text(), "test-token")

    def test_bind_mounts_do_not_require_a_version(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.render_nodes(directory, {"CL_1": None}, copy=False)["CL_1"]["ceos0"]
            self.assertNotIn("copy-to-flash", node)
            self.assertNotIn("extras", node)
            self.assertIn("CL_1_mappings/ceos0.json:/mnt/flash/EosIntfMapping.json:ro", node["binds"])
            self.assertIn("local-data:/data:ro", node["binds"])

    def test_semantic_version_boundaries(self):
        action = SIMULATION.ActionModule.__new__(SIMULATION.ActionModule)
        for version, expected in (
            ("0.9.0", False), ("0.80.99", False), ("0.81", True),
            ("0.81.0", True), ("v0.81.1", True), ("0.81.0-rc1", True),
            ("0.100.0+build.1", True), ("1.0.0", True),
        ):
            with self.subTest(version=version):
                self.assertEqual(action._uses_node_copy_to_flash(version, "CL_1"), expected)


if __name__ == "__main__":
    unittest.main()
