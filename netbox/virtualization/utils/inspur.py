import os
import urllib3
import requests
from django.db.models import Q
from django.db.utils import IntegrityError
from virtualization.models import VirtualMachine
from virtualization.models.clusters import Cluster
from ipam.models import IPAddress


def fetch_vms_form_inspur_cloud() -> list:
    inspur_cloud_url = os.getenv('INSPUR_CLOUD_URL', 'https://localhost')
    inspur_cloud_access_key = os.getenv('INSPUR_CLOUD_ACCESS_KEY', 'access_key')
    inspur_cloud_access_secret = os.getenv('INSPUR_CLOUD_ACCESS_SECRET', 'access_secret')
    inspur_cloud_host = os.getenv('INSPUR_CLOUD_HOST', '')

    headers = {
        # 'Cookie': os.getenv('INSPUR_COOKIE', ''),
        'version': "5.8",
        'Cache-Control': 'no-cache',
        'Authorization': f'ICS {inspur_cloud_access_key}:{inspur_cloud_access_secret}',
    }

    if inspur_cloud_host:
        headers['Host'] = inspur_cloud_host

    vms = []
    urllib3.disable_warnings()

    try:
        # resp = requests.get('https://192.168.20.35/vms?pageSize=1000&currentPage=1&sortField=&sort=desc',
        resp = requests.get(
            f'{inspur_cloud_url}/vms?pageSize=1000&currentPage=1&sortField=&sort=desc', headers=headers, verify=False
        )
        if resp.status_code == 200:
            data = resp.json()
            for vm in data['items']:
                info = {
                    'id': vm["id"],
                    'name': vm['name'],
                    'status': 'active' if vm['status'] == "STARTED" else "offline",
                    'vcpus': vm['cpuNum'],
                    'memory': vm['memory'],
                    'host': vm['hostIp'],
                }

                # 只获取配置了IP地址的虚拟机
                if len(vm['nics']) > 0:
                    ip_text = vm['nics'][0]['ip']

                    if ip_text:
                        info['ip'] = ip_text.split(',')[0]
                    else:
                        print(f"🐛 IP address not found for VM {vm['name']}, ignore")
                        continue
                else:
                    print(f"🐛NIC not found for VM {vm['name']}, skip")
                    continue

                vms.append(info)
            return vms
        else:
            print(f"🐞 Invoke Inspur API error: status({resp.status_code}), response({resp.text})")
            return []
    except requests.exceptions.ConnectionError as err:
        print(f"🐞 Connect Inspur API error: {str(err)}")
        return []


def sync_inspur(vms: list, cluster: Cluster):

    # Variable(tmp) to store vm instance in database
    vm_inst = VirtualMachine()
    # counter
    updated_count = 0
    created_count = 0
    ignored_list = []

    for vm in vms:
        host_ip_address = f"{vm['host']}/24"
        try:
            ip_obj = IPAddress.objects.get(address=host_ip_address)
            interface = ip_obj.assigned_object

            # 检查接口是否存在并且是否关联到物理设备
            if interface and hasattr(interface, 'device'):
                device = interface.device

                # 以下为浪潮虚拟机添加逻辑
                vm_name = vm['name']
                vm_ip = vm['ip']

                try:
                    ipaddr, created = IPAddress.objects.get_or_create(address=vm_ip)
                except IPAddress.MultipleObjectsReturned:
                    # Potential issue when there are 10.10.10.10/32 and 10.10.10.10/24 in database
                    ipaddr = IPAddress.objects.filter(address=vm_ip).first()

                # Try find by inspur cloud vm id
                vm_in_db = VirtualMachine.objects.filter(custom_field_data__cloud_vm_id=vm["id"])

                if not vm_in_db:
                    print(f"🚨 VM({vm['name']}, {vm['ip']}) not found by id {vm['id']}, use name/ip method")
                    # Try find VM by name, ip address in description or ip address equals primary_ip4
                    vm_in_db = VirtualMachine.objects.filter(
                        Q(name__icontains=vm_name) | (Q(description=vm_ip) | Q(primary_ip4=ipaddr)),
                        cluster=cluster,
                        # Q(name__icontains=vm_name) &
                        # Q(description__icontains=vm_ip) |
                        # Q(primary_ip4=ipaddr),
                        # cluster=cluster,
                    )

                if vm_in_db.count() == 0:
                    print(f"✔️ Inspur cloud VM({vm_name}) with IP({vm_ip}) not found in DB, ADDING")

                    vm_instance = VirtualMachine()
                    vm_instance.name = vm_name
                    vm_instance.status = vm['status']

                    # IP地址文本存放在描述里(临时兼容)
                    vm_instance.description = vm_ip
                    vm_instance.primary_ip4 = ipaddr

                    if vm.get('vcpus', 0):
                        vm_instance.vcpus = vm['vcpus']
                    if vm.get('memory', 0):
                        vm_instance.memory = vm['memory']

                    vm_instance.cluster = cluster

                    # Bind vm to device(physical machine)
                    vm_instance.device = device

                    # Bind vm to cloud vm id
                    vm_instance.custom_field_data['cloud_vm_id'] = vm['id']

                    vm_instance.save()
                    created_count += 1

                elif vm_in_db.count() > 1:
                    print(
                        f"🚨 Found multiple VMs(count: {vm_in_db.count()}) from netbox database by {vm_name}(IP: {vm_ip}), checking",
                    )
                    for item in vm_in_db:
                        print(item.name)

                    ignored = True
                    ignored_vm_name = ""
                    for dup_vm in vm_in_db:
                        if (
                            dup_vm.primary_ip4
                            and (vm_ip == str(dup_vm.primary_ip4.address.ip))
                            and vm_name == dup_vm.name
                        ):
                            print(f"Matched VM {dup_vm.name}(IP: {vm_ip}) by primary_ip4 ")
                            count = update_inspur_vm(dup_vm, vm, ipaddr)
                            updated_count += count
                            ignored = False
                            break
                        elif vm_ip in dup_vm.description and vm_name == dup_vm.name:
                            print(f"Matched VM {dup_vm.name}(IP: {vm_ip}) by description")
                            count = update_inspur_vm(dup_vm, vm, ipaddr)
                            updated_count += count
                            ignored = False
                            break

                    if ignored:
                        ignored_list.append(dup_vm.name)
                else:
                    # Found 1 VM from netbox database, update
                    vm_inst = vm_in_db.first()
                    count = update_inspur_vm(vm_inst, vm, ipaddr)
                    updated_count += count
            else:
                print(f"Device with IP address({host_ip_address}) not found, skip")
        except IPAddress.DoesNotExist:
            print(f"Device IP address not found {host_ip_address}, skip")
            continue
        except KeyError as err:
            print(f"KeyError when processing {vm}, skip")
            continue
        # except IntegrityError as err:
        #     print(f"IntegrityError when processing {vm}, skip: {err}")
        #     # Use SQL below to check:
        #     # SELECT * FROM public.virtualization_virtualmachine where primary_ip4_id=<IPAddress ID>
        #     continue

    # Print stale VMs(possible)
    cloud_vms = set([cloud_vm['name'] for cloud_vm in vms])
    db_vms = set([db_vm.name for db_vm in VirtualMachine.objects.filter(cluster=cluster)])

    if len(cloud_vms) > len(db_vms):
        print(f"🐛🐛 STALE VMs: {cloud_vms - db_vms}")
    else:
        print(f"🐛🐛 STALE VMs: {db_vms - cloud_vms}")

    print(
        f"🐛🐛 Result: Total: {len(vms)}, Created: {created_count}, Updated: {updated_count}, Ignored: {len(ignored_list)}"
    )
    for ig in ignored_list:
        print(ig)


def update_inspur_vm(vm_inst: VirtualMachine, data: dict, ipaddr: IPAddress) -> int:
    '''
    Return:
        1: updated
        0: no update
    '''
    vm_name = data['name']
    vm_ip = data['ip']

    update_flag = False

    # Logic to update vm instance in database
    # Update name
    if vm_inst.name != vm_name:
        print(
            f"🐛 Name of VM({vm_inst.name}: {vm_inst.description}[Database]) is different from VM({vm_name}: {vm_ip}[Cloud]), update"
        )
        vm_inst.name = vm_name
        update_flag = True

    # Update IP
    if vm_ip not in vm_inst.description:
        print(f"🐛 Update VM({vm_inst.name}) description to '{vm_ip}'")
        vm_inst.description = vm_ip
        update_flag = True

    if (not vm_inst.primary_ip4) or (vm_ip != str(vm_inst.primary_ip4.address.ip)):
        vm_inst.primary_ip4 = ipaddr
        print(f"🐛 Update VM({vm_inst.name}) primary_ip4 to '{vm_ip}'")
        update_flag = True

    if not vm_inst.custom_field_data["cloud_vm_id"]:
        print(f"🐛 Add cloud_vm_id({data['id']}) primary_ip4 to '{vm_inst.name}'")
        vm_inst.custom_field_data["cloud_vm_id"] = data["id"]
        update_flag = True

    # Save to DB
    if update_flag:
        vm_inst.status = data['status']
        vm_inst.save()
        return 1

    return 0
