"""Regression tests for physical link discovery and explicit external cables."""

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

    def render_topology(self, directory, mapping, same_number=True, parents=True, one_container=False, external_links=None):
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


if __name__ == "__main__":
    unittest.main()
