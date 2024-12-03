from __future__ import absolute_import, division, print_function
__metaclass__ = type
import json
import os
import yaml
import netaddr
import shutil
import glob

from string import Template
from ansible.plugins.action import ActionBase
from ansible.errors import AnsibleError

role_path = os.path.dirname(os.path.realpath(__file__)) + '/..'

class ActionModule(ActionBase):
    def dedup_connection(self, connection_list, connection) -> bool:
        reversed_connection = {"loc_switch":connection["peer_name"], "loc_int":connection["peer_int"], "peer_name":connection["loc_switch"], "peer_int":connection["loc_int"]}
        return reversed_connection in connection_list
    
    def unique(self, sequence):
        result = []
        for item in sequence:
            if item not in result:
                result.append(item)
        return result
    
    def create_clab_topology_links(self, sim_connections, containerlab_custom_interface_mapping, switch_intf_mapping_dict, inventory) -> dict:
        links_dict = {}
        
        for distributed_node in sim_connections:
            node_string = ""
            for connection in sim_connections[distributed_node]['local_con']:
                node_string += "    - endpoints: [\""+connection["loc_switch"]+":"+connection["loc_int"]+"\", \""+connection["peer_name"]+":"+connection["peer_int"]+"\"]\n"
            for connection in sim_connections[distributed_node]['local_vx_con']:
                connection_list = list(connection)
                node_string += "    - endpoints: [\""+connection_list[0]+":"+connection[connection_list[0]]+"\", \""+connection_list[1]+"-"+connection[connection_list[1]]+"\"]\n"

            links_dict[distributed_node] = node_string
        
        return links_dict
            
        
        
    def create_clab_topology_nodes(self, distributed_nodes, hostvars, containerlab_enforce_startup_config, containerlab_deploy_startup_batches,
                             containerlab_custom_interface_mapping, containerlab_onboard_to_cvp_token, containerlab_serial_sysmac, inventory) -> dict:
        nodes_dict = {}
        # Create the nodes for Clab topology file
        for distributed_node in distributed_nodes:  
            batch_count = 0 
            node_string = ""
            
            for node, kind_image in distributed_nodes[distributed_node].items():
                kind = kind_image["kind"]
                image = kind_image["image"]
                node_hostvars_exist = True
                try:
                    node_hostvar = hostvars[node]
                except Exception as e:
                    node_hostvars_exist = False
                    #raise AnsibleError("Node %s is not defined in the hostvars, most probably it is missing from the inventory.%s" % (node))
                
                node_string += "    "+ node + ":\n"
                node_string += "      kind: "+kind+"\n"
                node_string += "      image: "+image+"\n"
                
                if batch_count >= int(containerlab_deploy_startup_batches):
                    node_string += "      startup-delay: "+str((batch_count // int(containerlab_deploy_startup_batches)) * 300)+"\n"
                
                if node_hostvars_exist and kind in ["ceos","veos"]:
                    mgmt_ipv4 = ""
                    if "management_interfaces" in hostvars[node]:
                        first_entry = True
                        for mgmt_int in hostvars[node]["management_interfaces"]:
                            if first_entry:
                                if "type" in mgmt_int and "Vlan" not in mgmt_int["name"]:
                                    if mgmt_int["type"] == 'oob':
                                        mgmt_ipv4 = mgmt_int["ip_address"].split("/")[0]
                                        first_entry = False
                    else:
                        raise AnsibleError("Node %s has no defined management_interfaces." % node)
                    if mgmt_ipv4 != "":
                        node_string += "      mgmt-ipv4: "+mgmt_ipv4+"\n"
                    
                    if (kind in ["ceos","veos"]):    
                        node_string += "      startup-config: "+distributed_node+"_configs/"+node+".cfg\n"
                    
                    if containerlab_enforce_startup_config:
                        node_string += "      enforce-startup-config: true\n"
                    
                    if (kind in ["ceos","veos"]) and (containerlab_custom_interface_mapping or (containerlab_onboard_to_cvp_token is not None) or containerlab_serial_sysmac):
                        node_string += "      binds:\n"
                    if containerlab_custom_interface_mapping and node in inventory:
                        node_string += "        - "+distributed_node+"_mappings/"+node+".json:/mnt/flash/EosIntfMapping.json:ro\n"
                    if containerlab_onboard_to_cvp_token is not None:
                        node_string += "        - "+distributed_node+"_containerlab_onboarding_token:/mnt/flash/token:ro\n"
                    if containerlab_serial_sysmac and (kind in ["ceos","veos"]):
                        if "serial_number" in hostvars[node] or ("metadata" in hostvars[node] and "system_mac_address" in hostvars[node]["metadata"]):
                            node_string += "        - "+distributed_node+"_mappings/"+node+"_ceos_config:/mnt/flash/ceos-config:ro\n"
                    
                    if "containerlab" in hostvars[node]:
                        if "bind" in hostvars[node]["containerlab"]:
                            node_string +=  "        - "+str(hostvars[node]["containerlab"]["bind"])+"\n"
                        else:
                            node_string += "      "+str(hostvars[node]["containerlab"])+"\n"
                            
                elif node_hostvars_exist and kind in ["linux"]:
                    lines = str(hostvars[node]["clab_vars"]).splitlines()
                    for line in lines:
                        node_string += "      "+str(line)+"\n"
                
                batch_count += 1
            
            
            nodes_dict[distributed_node] = node_string
            
        return nodes_dict
    
    def run(self, tmp=None, task_vars=None):
        super(ActionModule, self).run(tmp, task_vars)
        ret = dict()
        inventory = sorted(task_vars.get("ansible_play_hosts_all", {}))
        hostvars = task_vars.get("hostvars", {})
        groups = task_vars.get("groups", {})

        # Default variables
        sim_env = "clab"
        vxlan_base = 100
        sim_include_avd_external_nodes = False
        sim_external_nodes_map = {}
        sim_ceos_version = "ceos:latest"
        sim_topology_file_name = "topology"
        sim_external_node_one_container = False
        containerlab_custom_interface_mapping = False
        containerlab_serial_sysmac = False
        containerlab_labname = "AVD"
        containerlab_prefix = "__lab-name"
        containerlab_enforce_startup_config = False
        containerlab_deploy_startup_batches = 20
        containerlab_onboard_to_cvp_token = None
        containerlab_mgmt_network_name = "MGMT"
        containerlab_mgmt_network = None
        containerlab_linux_kind_bind_dir = None
        
        first_sim_node = None
        all_sim_nodes = []
        if "SIMULATION" in groups:
            first_sim_node = groups["SIMULATION"][0] if groups["SIMULATION"][0] else None
            if len(groups["SIMULATION"]) > 1 and sim_env == "clab":
                all_sim_nodes = groups["SIMULATION"]
            else:
                all_sim_nodes.append(first_sim_node)

        if first_sim_node:
            inventory_dir = hostvars[first_sim_node].get("sim_inventory_dir_override", hostvars[first_sim_node]["inventory_dir"])
            global role_path
            if "playbook_dir_override" in hostvars[first_sim_node]:
                role_path = hostvars[first_sim_node]["playbook_dir_override"]
            if "sim_env" in hostvars[first_sim_node]:
                sim_env = hostvars[first_sim_node]["sim_env"]
            if "sim_include_avd_external_nodes" in hostvars[first_sim_node]:
                sim_include_avd_external_nodes = hostvars[first_sim_node]["sim_include_avd_external_nodes"]
            if "sim_external_nodes_map" in hostvars[first_sim_node]:
                sim_external_nodes_map = hostvars[first_sim_node]["sim_external_nodes_map"]
            if "sim_external_node_one_container" in hostvars[first_sim_node]:
                sim_external_node_one_container = hostvars[first_sim_node]["sim_external_node_one_container"]
            if "sim_ceos_version" in hostvars[first_sim_node]:
                sim_ceos_version = hostvars[first_sim_node]["sim_ceos_version"]
            if "sim_topology_file_name" in hostvars[first_sim_node]:
                sim_topology_file_name = hostvars[first_sim_node]["sim_topology_file_name"]
            if "sim_configs_dir" in hostvars[first_sim_node]:
                sim_configs_dir = hostvars[first_sim_node]["sim_configs_dir"]
            else:
                sim_configs_dir = inventory_dir + "/intended/configs/"

            if "containerlab_custom_interface_mapping" in hostvars[first_sim_node]:
                containerlab_custom_interface_mapping = hostvars[first_sim_node]["containerlab_custom_interface_mapping"]
            if "containerlab_vxlan_base" in hostvars[first_sim_node]:
                vxlan_base = hostvars[first_sim_node]["containerlab_vxlan_base"]
            if "containerlab_enforce_startup_config" in hostvars[first_sim_node]:
                containerlab_enforce_startup_config = hostvars[first_sim_node]["containerlab_enforce_startup_config"]
            if "containerlab_deploy_startup_batches" in hostvars[first_sim_node]:
                containerlab_deploy_startup_batches = hostvars[first_sim_node]["containerlab_deploy_startup_batches"]
            if "containerlab_labname" in hostvars[first_sim_node]:
                containerlab_labname = hostvars[first_sim_node]["containerlab_labname"]
            if "containerlab_prefix" in hostvars[first_sim_node]:
                containerlab_prefix = hostvars[first_sim_node]["containerlab_prefix"]
            if "containerlab_onboard_to_cvp_token" in hostvars[first_sim_node]:
                containerlab_onboard_to_cvp_token = hostvars[first_sim_node]["containerlab_onboard_to_cvp_token"]
            if "containerlab_serial_sysmac" in hostvars[first_sim_node]:
                containerlab_serial_sysmac = hostvars[first_sim_node]["containerlab_serial_sysmac"]
            if "containerlab_mgmt_network_name" in hostvars[first_sim_node]:
                containerlab_mgmt_network_name = hostvars[first_sim_node]["containerlab_mgmt_network_name"]
            if "containerlab_mgmt_network" in hostvars[first_sim_node]:
                containerlab_mgmt_network = hostvars[first_sim_node]["containerlab_mgmt_network"]
            if "containerlab_linux_kind_bind_dir" in hostvars[first_sim_node]:
                containerlab_linux_kind_bind_dir = hostvars[first_sim_node]["containerlab_linux_kind_bind_dir"]
            if "containerlab_dir" in hostvars[first_sim_node]:
                containerlab_dir = hostvars[first_sim_node]["containerlab_dir"]
            else:
                containerlab_dir = inventory_dir + "/intended/containerlab/"
            if "containerlab_add_mgmt_links" in hostvars[first_sim_node]:
                containerlab_add_mgmt_links = hostvars[first_sim_node]["containerlab_add_mgmt_links"]
            else:
                containerlab_add_mgmt_links = []
        
        # If sim_env not clab only one simulation node is possible, ex. for ACT. The first node will be choosen
        if sim_env != "clab":
            all_sim_nodes = [first_sim_node]
        
        
        avd_connections = []
        ext_connections = []
        ext_nodes = []
        switch_intf_mapping_dict = {}
        host_count = len(all_sim_nodes)
        host_distribute_counter = 1
        distributed_nodes = {}
        sim_connections = {}
        client_all_intf_index = 1
        client_all_added = False
        
        for sim_node in all_sim_nodes:
            # dict for every sim node to hold the switches and external devices it has to run
            distributed_nodes[sim_node] = {}
            # Create structure to hold the connections per sim node
            sim_connections.update({ sim_node:{'local_con':[], 'local_vx_con':[], 'local_vx_clab_tools_cmd':[]} })
        
        for switch in inventory:
            
            # Ignore switches which are not deployed
            if "is_deployed" in hostvars[switch] and hostvars[switch]["is_deployed"] == False:
                    continue

            # if there are more than 1 clab host defined, create a list on which host which switch will run on
            if host_count > 1:
                position = host_distribute_counter % (host_count)
                distributed_nodes[(list(distributed_nodes)[position])][switch] = {"kind": "ceos", "image":sim_ceos_version}
            else:
                distributed_nodes[(list(distributed_nodes)[0])][switch] = {"kind": "ceos", "image":sim_ceos_version}
            host_distribute_counter += 1


            if "ethernet_interfaces" in hostvars[switch]:

                intf_counter = 1
                switch_intf_mapping_dict[switch] = {}
                for eth in hostvars[switch]["ethernet_interfaces"]:
                    
                    # Ignore ethernet interfaces which don't have a peer_interface defined, this is for example when using eos_designs -> network_ports
                    if "peer_interface" in eth:

                        # Ignore connections to switches which are not deployed
                        if eth["peer"] in hostvars:
                            if "is_deployed" in hostvars[eth["peer"]] and hostvars[eth["peer"]]["is_deployed"] == False:
                                continue
                        
                        # create EOS interface to linux eth mapping if needed
                        if containerlab_custom_interface_mapping:
                            tmp_intf_mapping = {str((eth["name"].split("."))[0]):"eth"+str(intf_counter)}
                            switch_intf_mapping_dict[switch].update(tmp_intf_mapping)
                            intf_counter += 1

                        # if sim_env is 'clab' replace 'Ethernet' with 'eth' and '/' with '_'      
                        if sim_env == "clab" and not containerlab_custom_interface_mapping:
                            loc_int = str((eth["name"].split("."))[0]).replace("Ethernet","eth").replace("/","_")
                            peer_int = str((eth["peer_interface"].split("."))[0]).replace("Ethernet","eth").replace("/","_")
                        else:
                            loc_int = str((eth["name"].split("."))[0])
                            peer_int = str((eth["peer_interface"].split("."))[0])

                        connection = {"loc_switch":switch, "loc_int":loc_int, "peer_name":eth["peer"], "peer_int":peer_int}

                        # Check if connection is not in the connections list already
                        if not self.dedup_connection(avd_connections, connection):
                            if eth["peer"] in inventory:
                                # Connections between AVD defined nodes
                                avd_connections.append(connection)            
                            else:
                                # Connections to external nodes (ex. defined in connected endpoints)
                                if sim_include_avd_external_nodes:
                                    if "peer_type" in eth:
                                        peer_type = eth["peer_type"]
                                    else:
                                        peer_type = "none"
                                    
                                    if sim_external_node_one_container:
                                        # If there should be only one external node representing all external connection (today only ceos)
                                        client_all_connection = {"loc_switch":switch, "loc_int":loc_int, "peer_name":"client_all", "peer_int":"eth"+str(client_all_intf_index)}
                                        client_all_intf_index += 1
                                        ext_connections.append(client_all_connection)
                                    else:
                                        for tmp_connection in ext_connections:
                                            if (str(connection["peer_int"]).lower() == tmp_connection["peer_int"]) and (connection["peer_name"] == tmp_connection["peer_name"]):
                                                connection["peer_int"] = "eth"+str(int(connection["peer_int"].split("eth")[1]) + 1)
                                            else:
                                                connection["peer_int"] = str(connection["peer_int"])
                                        
                                        connection["peer_int"] = str(connection["peer_int"].lower())
                                        ext_connections.append(connection)
                                    
                                    # Add the external nodes to a set so that they can be distributed to the simulation hosts afterwards
                                    node_type = {"kind": "ceos", "image":sim_ceos_version}
                                    if peer_type in sim_external_nodes_map and not sim_external_node_one_container:
                                        node_type = sim_external_nodes_map[peer_type]
                                    
                                    if sim_external_node_one_container and not client_all_added:
                                        # If there should be only one external node representing all external connection (today only ceos)
                                        ext_nodes.append({"client_all":node_type})
                                        client_all_added = True
                                    elif not sim_external_node_one_container:
                                        ext_nodes.append({eth["peer"]:node_type})

        # Additional containerlab mgmt connection defined in vars of SIMULATION Host                         
        for element in containerlab_add_mgmt_links:
            tmp_conn = {"loc_switch":element["node"], "loc_int":element["intf"], "peer_name":"mgmt-net", "peer_int":element["node"]+"-"+element["intf"]}
            # print (tmp_conn)
            ext_connections.append(tmp_conn)

        # Distribute the external nodes to the simulation hosts afterwards                        
        if sim_include_avd_external_nodes: 
            for ext_node in self.unique(ext_nodes):
                if host_count > 1:
                    position = host_distribute_counter % (host_count)
                    distributed_nodes[(list(distributed_nodes)[position])].update(ext_node)
                else:
                    distributed_nodes[(list(distributed_nodes)[0])].update(ext_node)
                host_distribute_counter += 1                       


        # Create a connections list for each simulation node
        connections = avd_connections
        if sim_include_avd_external_nodes:
            connections = avd_connections + ext_connections
        
        # set linux interface if clab intf mapping is set
        modified_connections = []
        if containerlab_custom_interface_mapping and sim_env == "clab":
            for connection in connections:
                mod_loc_int = connection["loc_int"]
                mod_peer_int = connection["peer_int"]
                if connection["loc_switch"] in inventory:
                    mod_loc_int = switch_intf_mapping_dict[connection["loc_switch"]][connection["loc_int"]]
                else:
                    mod_loc_int = mod_loc_int.split(".")[0].replace("Ethernet","eth").replace("/","_")
                if connection["peer_name"] in inventory:
                    mod_peer_int = switch_intf_mapping_dict[connection["peer_name"]][connection["peer_int"]]
                else:
                    mod_peer_int = mod_peer_int.split(".")[0].replace("Ethernet","eth").replace("/","_")

                modified_connection = {"loc_switch":connection["loc_switch"], "loc_int":mod_loc_int,"peer_name":connection["peer_name"], "peer_int":mod_peer_int}
                modified_connections.append(modified_connection)

        else:
            modified_connections = connections
        
        vxlan_count = vxlan_base
        for sim_node in all_sim_nodes:

            for node in distributed_nodes[sim_node]:

                for connection in modified_connections:

                    if node == connection["loc_switch"]:

                        # Connections which stay local on the simulation host (both nodes on the same simulation host)
                        if connection["peer_name"] in distributed_nodes[sim_node]:
                            sim_connections[sim_node]["local_con"].append(connection)

                        # Additional containerlab mgmt connection defined in vars of SIMULATION Host
                        elif connection["peer_name"] == "mgmt-net":
                            sim_connections[sim_node]["local_con"].append(connection)
                            
                        # Connections which are to a remote simulation node running on a different simulation host (written in local topology file)
                        else:
                            sim_connections[sim_node]["local_vx_con"].append({connection["loc_switch"]:connection["loc_int"], "host:vx" + str(vxlan_count):connection["peer_int"]})
                            # Create the containerlab vxlan mapping commands (belong to the before created vxlan connection for the topology file)
                            for tmp_sim_node in all_sim_nodes:
                                # Find the containerlab host IP where the remote node will be running
                                if connection["peer_name"] in distributed_nodes[tmp_sim_node]:
                                    # Create the vxlan contaierlab command for the remote containerlab host (perspective from the local sim node)
                                    sim_connections[sim_node]["local_vx_clab_tools_cmd"].append("clab tools vxlan create --remote " + hostvars[tmp_sim_node]["ansible_host"] + " --id " + str(vxlan_count) + " --link vx" + str(vxlan_count) + "-" + str(connection["peer_int"]))
                                    # Create the vxlan connection and vxlan contaierlab command for the remote containerlab host (perspective from the remote sim node)
                                    sim_connections[tmp_sim_node]["local_vx_con"].append({connection["peer_name"]:connection["peer_int"], "host:vx" + str(vxlan_count):connection["loc_int"]})
                                    sim_connections[tmp_sim_node]["local_vx_clab_tools_cmd"].append("clab tools vxlan create --remote " + hostvars[sim_node]["ansible_host"] + " --id " + str(vxlan_count) + " --link vx" + str(vxlan_count) + "-" + str(connection["loc_int"]))
                        
                        vxlan_count += 1    
                        

        
        # Generate the intf mapping files if needed
        mapping_switch_mapping_dict = {}
        mapping_switch_all = {}
        if containerlab_custom_interface_mapping:
            first_switch = True
            for switch in switch_intf_mapping_dict:   
                eth_mapping_switch = {}
                ma_mapping_switch = {}
                if "management_interfaces" in hostvars[switch]:
                    first_entry = True
                    for mgmt_int in hostvars[switch]["management_interfaces"]:
                      if first_entry:
                          if "type" in mgmt_int and "Vlan" not in mgmt_int["name"]:
                            if mgmt_int["type"] == 'oob':
                                ma_mapping_switch.update({ 'eth0':mgmt_int["name"] })
                                first_entry = False
                                
                                # Try do determine the 'containerlab_mgmt_network' if it is not defined in the inventory
                                if containerlab_mgmt_network is None and first_switch:
                                    first_switch = False
                                    containerlab_mgmt_network = netaddr.IPNetwork(mgmt_int["ip_address"]).cidr
                for key, value in switch_intf_mapping_dict[switch].items():
                    eth_mapping_switch.update({ value:key })
                    
                mapping_switch_all = {'ManagementIntf':ma_mapping_switch}
                mapping_switch_all.update({'EthernetIntf':eth_mapping_switch})
                mapping_switch_mapping_dict[switch] = mapping_switch_all
        
        # Create the topology file(s) and vxlan command scripts (if more than one clab host)
        # Create folders for the simulation nodes and put the needed files in there
        nodes_dict = {}
        links_dict = {}
        if sim_env == "clab":
            nodes_dict = self.create_clab_topology_nodes(distributed_nodes, hostvars, containerlab_enforce_startup_config, containerlab_deploy_startup_batches,
                             containerlab_custom_interface_mapping, containerlab_onboard_to_cvp_token, containerlab_serial_sysmac, inventory)
            links_dict = self.create_clab_topology_links(sim_connections, containerlab_custom_interface_mapping, switch_intf_mapping_dict, inventory)
             
            
            for node in distributed_nodes:
                # Create the CVP onboarding token if it is defined
                if containerlab_onboard_to_cvp_token is not None:
                    cvp_token_filename = containerlab_dir+node+"_containerlab_onboarding_token"
                    os.makedirs(os.path.dirname(cvp_token_filename), exist_ok=True)
                    with open(cvp_token_filename, "w") as fh:
                        fh.write(containerlab_onboard_to_cvp_token)
                                    
                # Create Containerlab Topo
                topo_substitute = {
                    'containerlab_labname': containerlab_labname,
                    'containerlab_prefix': containerlab_prefix,
                    'containerlab_mgmt_network_name': containerlab_mgmt_network_name,
                    'containerlab_mgmt_network': containerlab_mgmt_network,
                    'nodes': nodes_dict[node],
                    'links': links_dict[node]
                }

                with open(f'{role_path}/templates/containerlab_topology.txt', 'r') as f:
                    src = Template(f.read())
                    result = src.substitute(topo_substitute)
                
                filename = containerlab_dir+node+"_"+sim_topology_file_name+".yml"
                os.makedirs(os.path.dirname(filename), exist_ok=True)
                with open(filename, "w") as fh:
                    fh.write(result)

                if len(distributed_nodes) > 1:
                    # Create vxlan command script
                    vxlan_script_substitute = {
                        'connection': "\n".join(sim_connections[node]["local_vx_clab_tools_cmd"])
                    }
                    with open(f'{role_path}/templates/containerlab_vxlan_commands.txt', 'r') as f:
                        src = Template(f.read())
                        vxlan_script_result = src.substitute(vxlan_script_substitute)

                    vxlan_script_filename = containerlab_dir+node+"_vxlan_commands.sh"
                    os.makedirs(os.path.dirname(vxlan_script_filename), exist_ok=True)
                    with open(vxlan_script_filename, "w") as fh:
                        fh.write(vxlan_script_result)
                    
                    
                    # Create vxlan delete command script
                    delete_clab_tools_connection = []
                    for del_connection in sim_connections[node]["local_vx_clab_tools_cmd"]:
                        delete_clab_tools_connection.append("ip link delete " + del_connection.split("--link")[1].strip())
                        delete_clab_tools_connection.append("ip link delete vx-" + del_connection.split("--link")[1].strip())
                    vxlan_delete_script_substitute = {
                        'delete_clab_tools_connection': "\n".join(delete_clab_tools_connection)
                    }
                    with open(f'{role_path}/templates/containerlab_vxlan_interface_delete.txt', 'r') as f:
                        src = Template(f.read())
                        vxlan_delete_script_result = src.substitute(vxlan_delete_script_substitute)

                    vxlan_delete_script_filename = containerlab_dir+node+"_vxlan_interface_delete.sh"
                    os.makedirs(os.path.dirname(vxlan_delete_script_filename), exist_ok=True)
                    with open(vxlan_delete_script_filename, "w") as fh:
                        fh.write(vxlan_delete_script_result)
                        
                    
                # Create config and mapping directories for each simulation nodes and put the needed files into these directories
                os.makedirs(os.path.dirname(containerlab_dir+node+"_configs/"), exist_ok=True)
                
                if containerlab_custom_interface_mapping:
                    os.makedirs(os.path.dirname(containerlab_dir+node+"_mappings/"), exist_ok=True)
                               
                # Copy config files and create mapping files per simulation node
                for switch in distributed_nodes[node]:
                    if switch in hostvars:
                        orig_config = sim_configs_dir+switch+".cfg"
                        copy_config = containerlab_dir+node+"_configs/"+switch+".cfg"
                        if os.path.exists(orig_config):
                            shutil.copy(orig_config, copy_config)
                    
                    if containerlab_custom_interface_mapping and switch in inventory:
                        filename = containerlab_dir+node+"_mappings/"+switch+".json"
                        with open(filename, 'w') as file:
                            json_string = json.dumps(mapping_switch_mapping_dict[switch], sort_keys=True, indent=4)
                            file.write(json_string)
                    
                    # Create the ceos-config with serial number and/or system mac address if defined
                    if containerlab_serial_sysmac:
                        if switch in hostvars:
                            if "serial_number" in hostvars[switch] or ("metadata" in hostvars[switch] and "system_mac_address" in hostvars[switch]["metadata"]):
                                filename = containerlab_dir+node+"_mappings/"+switch+"_ceos_config"
                                with open(filename, 'w') as file:
                                    if "serial_number" in hostvars[switch]:
                                        file.write("SERIALNUMBER="+hostvars[switch]["serial_number"])
                                    if "metadata" in hostvars[switch]:
                                        if "system_mac_address" in hostvars[switch]["metadata"]:
                                            file.write("\nSYSTEMMACADDR="+hostvars[switch]["metadata"]["system_mac_address" ])
                
            # Package all files needed for each simulation host
            for node in distributed_nodes:
                filenames = []
                for file_name in glob.glob(containerlab_dir+node+"*"):
                    if os.path.isfile(file_name):
                        filenames.append(file_name)
                folders = glob.glob(containerlab_dir+node+"*/")
                os.makedirs(os.path.dirname(containerlab_dir+node+"/"), exist_ok=True)
                for filename in filenames:
                    shutil.copy(filename, containerlab_dir+node+"/")
                for folder in folders:
                    shutil.copytree(folder, containerlab_dir+node+"/"+folder.split("/")[-2])
                # Copy the bind directory for endpoints of kind linux if defined
                if containerlab_linux_kind_bind_dir is not None:
                    bind_path = inventory_dir + "/" + containerlab_linux_kind_bind_dir
                    if os.path.exists(bind_path):
                        shutil.copytree(bind_path, containerlab_dir+node+"/"+containerlab_linux_kind_bind_dir)
                shutil.make_archive(containerlab_dir+node+"_tmp_containerlab_archive", 'gztar', containerlab_dir+node)
                
                # Cleanup temporary files
                shutil.rmtree(os.path.dirname(containerlab_dir+node+"/"))
                shutil.rmtree(os.path.dirname(containerlab_dir+node+"_configs/"))
                if containerlab_custom_interface_mapping:
                    shutil.rmtree(os.path.dirname(containerlab_dir+node+"_mappings/"))

        return dict(ansible_facts=dict(ret))
