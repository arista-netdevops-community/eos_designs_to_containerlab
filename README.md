# eos_designs to containerlab

**eos_designs_to_containerlab** is a role to build a [containerlab](https://containerlab.srlinux.dev/) topology from [Arista AVD](https://www.avd.sh) project

## Requirements

- General requirements are located here: [avd-requirements](../../README.md#Requirements)
- containerlab must be installed or a host with docker installed must be available in case containeriezed deployment will be used. See the following link for deployment models of containerlab: https://containerlab.dev/install/
- containerlab in version `>=0.25.0`
- The cEOS-lab image which should be used has to be available in the docker images of the host running containerlab..
- The structured configs and device configs must be generated by eos_designs and eos_cli_config_gen first.
- In case you want to split the AVD nodes to run on different containerlab hosts, the containerlab hosts must be able to reach each other (IP connectivity).
- NOTE: If you are running a cEOS-lab < 4.28.0F set "containerlab_custom_interface_mapping: false".
- NOTE: If want to use one cEOS to simulate all client connections you have to use cEOS-lab > 4.31.0F.
- NOTE: If you are running several containerlab topologies on one host using the clab vxlan tools you have to make sure that you don't have an overlap in the vni mappings. You can set the vni starting point for your lab with "containerlab_vxlan_base"

## Installation

Clone this project to your directory where your AVD playbooks are.

## Role Variables

The following default variables are defined, and can be modified as desired:

- `sim_dir`: Location for the generated files for the simulation environment. (default: '`{{ inventory_dir }}/intended/simulation/`')
- `sim_configs_dir`: Location for configruation files generated by AVD. (default: '`{{ inventory_dir }}/intended/configs/`')
- `sim_env`: Simulation enviroment you want to use, currently only 'clab' is supported. (default: clab)
- `sim_ceos_version`: Name of the cEOS-lab docker image to be used. (default: ceos:latest)
- `sim_topology_file_name`: Filename which will be used for the containerlab topology. (default: topology)
- `sim_include_avd_external_nodes`: Whether to add nodes that are not definded in the AVD fabric, ex L3 peers, server and other connected endpoints. (default: false)
- `sim_external_node_one_container`: Whether to simulate all external nodes with one ceos. (default: false)
  NOTE: If this is set to 'true' the 'sim_external_nodes_map' is ignored.
  NOTE: This should only be used with EOS >= 4.31.0F where you can change the MAC addresses for each interface.
- `sim_external_nodes_map:` For each AVD 'peer_type' that should be added, the simulation node_type which should be used (if nothing is defined ceos will be used). Example:

```
	sim_external_nodes_map:
       	  server:
            kind:'linux'
            image:'alpine:latest'
          others: 
            kind: 'ceos'
            image: 'ceos:4.31.1F'
```

- `containerlab_custom_interface_mapping`: Should a custom interface mapping be created based on the AVD defined interfaces, this option also changes the management interface to what is defined for the nodes in eos_designs. Supported in cEOS-lab 4.28.0F and later. Should be set to true if Management 1 is used as management interface. (default: false)
- `containerlab_labname`: Name of the containerlab lab. (default: AVD)
- `containerlab_prefix`: Prefix which will be used for the node containers, see https://containerlab.dev/manual/topo-def-file/#prefix. (default: __lab-name)
- `containerlab_mgmt_network_name`:  Name of the management network bridge. (default: MGMT)
  NOTE: If you are running multiple containerlab labs on the same host the management network name should be unique for each lab.
- `containerlab_mgmt_network`: Address range for the management network. Currently containerlab supports one management network. (if not set it will be tried to find the network range based on the first node which is processed.)

  NOTE: If you are running multiple containerlab labs on the same host the management network name should be unique for each lab.
- `containerlab_vxlan_base`: Base VXLAN VNI for the communication between containers on different containerlab hosts. (default: 100)
- `containerlab_enforce_startup_config`: Enforce to reset the startup-config for every run, see: https://containerlab.dev/manual/nodes/#enforce-startup-config . (default: true)
- `containerlab_mode`: Deploy the topology using an installed or containerized containerlab. (default: installed ; options: installed | container)
- `containerlab_timeout`: The timeout of API requests that containerlab send toward external resources (i.e. docker). (default: 120s)
- `containerlab_skip_post_deploy`: Skip containerlab post deploy actions. Supported in containerlab version 0.24 and later. (default: false)
- `containerlab_max_workers`: Limit the amout of concurrent workers that create containers or wire virtual links. (default: equals the number of nodes/links to create)
- `containerlab_onboard_to_cvp_token:` Optional parameter to provide a CVP token to devices for registering with CVP automatically.
- `containerlab_deploy_on_hosts:` Copy the topology file and other needed files to the containerlab hosts and run the lab. If set to false the topology files and other needed files will only be created in the intended/ folder. (default: true)
- `containerlab_deploy_startup_batches:` Depending on your containerlab host resources you might want to limit the amount of containers started in parallel. This option uses the containerlab option startup-delay and the delay is set to 300 seconds between the batches. (default: 20)
- `containerlab_endpoint_bind_dir:` In case the connected endpoints are of the kind 'linux' you can map a directory to an endpoint if needed. (default: None)

The above mentioned variables can be defined in the inventory file as shown below in the example playbook section.

### Additional containerlab variables for nodes

There is a possibility to define specific variables supported by containerlab, see https://containerlab.dev/manual/nodes/, per AVD node. Therefore you can use the structured_config option provided by eos_designs and eos_cli_config_gen.

The following example shows this possibility. In this case Spine_1 should start 30 seconds after the the other nodes. For this the containerlab option startup-delay (https://containerlab.dev/manual/nodes/#startup-delay) can be used.

```yaml
# Spine Switches
spine:
  defaults:
    ...
  node_groups:
    dc1_spines:
      ...
      nodes:
        Spine_1:
          id: 1
          mgmt_ip: 11.11.11.11/24
          ...
          structured_config:
            containerlab:
              startup-delay: 30
        Spine_2:
        ...
```

## Playbook

Here is a playbook example to use `arista.avd.eos_designs_to_containerlab`:

```yaml
---
- name: Create avd nodes specific files
  hosts: FABRIC
  gather_facts: false
  tasks:
    - name: 'Create avd nodes specific files'
      import_role:
        name: eos_designs_to_containerlab
        tasks_from: create_avd_node_files

- name: Deploy containerlab topology
  hosts: SIMULATION
  gather_facts: false
  tasks:
    - name: 'Create and deploy containerlab topology'
      import_role:
        name: eos_designs_to_containerlab
        tasks_from: deploy

```

## Inventory

Here is an example how to define two simulation hosts in the inventory file (the name "SIMULATION" must not be changed)

NOTE: The group SIMULATION and the mandatory fields have to be defined.

```yaml
---
all:
  children:
    SIMULATION:
      hosts:
        CL_1:
          ansible_host: 1.1.1.1
        CL_2:
          ansible_host: 1.1.1.2
      vars:
          ansible_user: user
          ansible_password: password
          ansible_become_password: password
          ansible_connection: ssh
          ansible_become: true
          ansible_become_method: sudo
          sim_env: clab
          sim_ceos_version: ceos:4.28.0F
          sim_topology_file_name: custom_topology.yml
          containerlab_vxlan_base: 200
          containerlab_labname: MyLabName
          containerlab_mgmt_network: 11.11.11.0/24
          containerlab_custom_interface_mapping: false                  # should be set to false if you are running a cEOS-lab < 4.28.0F
          containerlab_prefix: '""'
          containerlab_onboard_to_cvp_token: "eyJhbGciOiJSU..."         # you can provide a onboarding token from the CVP defined in the AVD eos_designs so that your virtual devices can directly onboard to this CVP
```

## License

Project is published under [Apache 2.0 License](../../blob/main/LICENSE)
