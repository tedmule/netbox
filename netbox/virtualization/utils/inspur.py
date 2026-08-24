import os
import urllib3
import requests
from django.db.models import Q
from django.db.utils import IntegrityError
from django.db import transaction
from virtualization.models import VirtualMachine
from virtualization.models.clusters import Cluster
from ipam.models import IPAddress
from virtualization.utils.util import extract_ip


def _fill_vm_info(vm_data: list) -> dict:
    vms = {}
    for vm in vm_data:
        vm_id = vm['id']
        info = {
            # 'id': vm["id"],
            'name': vm['name'],
            'status': 'active' if vm['status'] == "STARTED" else "offline",
            'vcpus': vm['cpuNum'],
            'memory': vm['memory'],
            'host': vm['hostIp'],
            'desc': vm['description'] if vm['description'] else '',
        }

        # 只获取配置了IP地址的虚拟机
        if len(vm['nics']) > 0:
            ip_text = vm['nics'][0]['ip']

            if ip_text:
                info['ip'] = ip_text.split(',')[0]
            else:
                print(f"🐛 IP address not found for VM {vm['name']}, set ip string empty")
                info['ip'] = ""
        else:
            print(f"🐛NIC not found for VM {vm['name']}, skip")
            continue

        vms[vm_id] = info
    return vms


def fetch_vms_form_inspur_cloud(page_size: int = 100) -> dict:
    """
    return dict
    """
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

    vms = {}
    current_page = 1
    urllib3.disable_warnings()

    try:
        # resp = requests.get('https://192.168.20.35/vms?pageSize=1000&currentPage=1&sortField=&sort=desc',
        resp = requests.get(
            f'{inspur_cloud_url}/vms?pageSize={page_size}&currentPage={current_page}&sortField=&sort=desc',
            headers=headers,
            verify=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            vm_dict = _fill_vm_info(data['items'])
            vms |= vm_dict
            while current_page < data['totalPage']:
                current_page += 1
                resp = requests.get(
                    f'{inspur_cloud_url}/vms?pageSize={page_size}&currentPage={current_page}&sortField=&sort=desc',
                    headers=headers,
                    verify=False,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    vm_dict = _fill_vm_info(data['items'])
                    vms |= vm_dict
                else:
                    print(f"🐞 Invoke Inspur API error: status({resp.status_code}), response({resp.text})")
            return vms
        else:
            print(f"🐞 Invoke Inspur API error: status({resp.status_code}), response({resp.text})")
            return vms
    except requests.exceptions.ConnectionError as err:
        print(f"🐞 Connect Inspur API error: {str(err)}")
        return {}


def sync_inspur(vms: dict, cluster: Cluster):

    # counter
    updated_count = 0
    created_count = 0
    skipped = []

    vms_in_db = VirtualMachine.objects.filter(cluster=cluster)

    vm_db_ids = set([vm.custom_field_data["cloud_vm_id"] for vm in vms_in_db])
    cloud_vm_ids = set(vms.keys())

    ids_to_delete = vm_db_ids - cloud_vm_ids
    print(f"Netbox中需要删除的虚拟机({len(ids_to_delete)}):")
    for vid in vm_db_ids - cloud_vm_ids:
        print(f"{vid}, deleted")
        try:
            VirtualMachine.objects.get(custom_field_data__cloud_vm_id=vid).delete()
        except VirtualMachine.MultipleObjectsReturned:
            print(f"❌ {vid} return multiple objects when deleting, skip")

    for vm_id, vm in vms.items():
        print(f"🐛 Processing VM {vm['name']} with Inspur ID {vm_id} in cluster {cluster}")
        # Get or create ip address for VM
        vm_name = vm['name']

        vm_ip = vm['ip']

        host_ip_address = f"{vm['host']}/24"
        print(f"Host IP address for VM {vm_name} is {host_ip_address}, checking if device exists in Netbox")

        if vm_ip:
            try:
                ipaddr, created = IPAddress.objects.get_or_create(address=vm_ip)
                print(f"IP address for VM {vm_name} is {ipaddr}, created: {created}")
            except IPAddress.MultipleObjectsReturned:
                # Potential issue when there are 10.10.10.10/32 and 10.10.10.10/24 in database
                ipaddr = IPAddress.objects.filter(address=vm_ip).first()
                print(f"Reuse first IP address({ipaddr})for VM {vm_name}")

        try:
            vm_in_db = VirtualMachine.objects.get(custom_field_data__cloud_vm_id=vm_id)
            print(f"Found {vm_in_db.name} in DB by Inspur ID {vm_id}, UPDATE.")

            count = update_inspur_vm(vm_in_db, vm)
            updated_count += count

        except VirtualMachine.DoesNotExist:
            print(f"✔️  No VM found by Inspur ID {vm_id} (Inspur cloud name: {vm['name']}), CREATE")

            try:
                ip_obj = IPAddress.objects.get(address=host_ip_address)
                interface = ip_obj.assigned_object

                # 检查接口是否存在并且是否关联到物理设备
                if interface and hasattr(interface, 'device'):
                    device = interface.device

                    vm_instance = VirtualMachine()
                    vm_instance.name = vm_name
                    vm_instance.status = vm['status']

                    vm_instance.description = vm.get('desc', '')
                    # IP地址文本存放在config_context的'_ip'字段里
                    if not vm_instance.local_context_data:
                        vm_instance.local_context_data = {}
                        vm_instance.local_context_data['_ip'] = vm_ip
                    else:
                        vm_instance.local_context_data['_ip'] = vm_ip

                    # BUG: 先注释掉IP地址绑定，后续优化后添加
                    # vm_instance.primary_ip4 = ipaddr

                    if vm.get('vcpus', 0):
                        vm_instance.vcpus = vm['vcpus']
                    if vm.get('memory', 0):
                        vm_instance.memory = vm['memory']

                    vm_instance.cluster = cluster

                    # Bind vm to device(physical machine)
                    vm_instance.device = device

                    # Bind vm to cloud vm id
                    vm_instance.custom_field_data['cloud_vm_id'] = vm_id

                    vm_instance.save()
                    created_count += 1
                else:
                    print(f"❌ Device with IP address({host_ip_address}) not found, skip")
                    skipped.append(vm)
                    continue
            except IPAddress.DoesNotExist:
                print(f"❌ Device IP address not found {host_ip_address}, skip")
                skipped.append(vm)
                continue
            except KeyError as err:
                print(f"❌ KeyError when processing {vm}, skip")
                skipped.append(vm)
                continue

    print(
        f"🐛🐛🐛 Result: Total: {len(vms)}, Created: {created_count}, Updated: {updated_count}, Ignored: {len(skipped)}"
    )
    for skp in skipped:
        print(skp)

    # ================ 以下为旧添加逻辑，后期优化后删除 ================
    # host_ip_address = f"{vm['host']}/24"
    # try:
    #    ip_obj = IPAddress.objects.get(address=host_ip_address)
    #    interface = ip_obj.assigned_object

    #    # 检查接口是否存在并且是否关联到物理设备
    #    if interface and hasattr(interface, 'device'):
    #        device = interface.device

    #        # 以下为浪潮虚拟机添加逻辑
    #        vm_name = vm['name']
    #        vm_ip = vm['ip']

    #        # Get or create ip address for VM
    #        try:
    #            ipaddr, created = IPAddress.objects.get_or_create(address=vm_ip)
    #        except IPAddress.MultipleObjectsReturned:
    #            # Potential issue when there are 10.10.10.10/32 and 10.10.10.10/24 in database
    #            ipaddr = IPAddress.objects.filter(address=vm_ip).first()

    #        # Try find VM by name, ip address in description or ip address equals primary_ip4
    #        vm_in_db = VirtualMachine.objects.filter(
    #            Q(name__icontains=vm_name) | (Q(description=vm_ip) | Q(primary_ip4=ipaddr)),
    #            cluster=cluster,
    #            # Q(name__icontains=vm_name) &
    #            # Q(description__icontains=vm_ip) |
    #            # Q(primary_ip4=ipaddr),
    #            # cluster=cluster,
    #        )

    #        if vm_in_db.count() == 0:
    #            print(f"✔️ Inspur cloud VM({vm_name}) with IP({vm_ip}) not found in DB, ADDING")

    #            vm_instance = VirtualMachine()
    #            vm_instance.name = vm_name
    #            vm_instance.status = vm['status']

    #            # IP地址文本存放在描述里(临时兼容)
    #            vm_instance.description = vm_ip
    #            # IP地址文本存放在config_context里(临时兼容)
    #            vm_instance.local_context_data['_ip'] = vm_ip
    #            vm_instance.primary_ip4 = ipaddr

    #            if vm.get('vcpus', 0):
    #                vm_instance.vcpus = vm['vcpus']
    #            if vm.get('memory', 0):
    #                vm_instance.memory = vm['memory']

    #            vm_instance.cluster = cluster

    #            # Bind vm to device(physical machine)
    #            vm_instance.device = device

    #            # Bind vm to cloud vm id
    #            vm_instance.custom_field_data['cloud_vm_id'] = vm_id

    #            vm_instance.save()
    #            created_count += 1

    #        elif vm_in_db.count() > 1:
    #            print(
    #                f"🚨 Found multiple VMs(count: {vm_in_db.count()}) from netbox database by {vm_name}(IP: {vm_ip}), checking",
    #            )
    #            for vm_item in vm_in_db:
    #                print(vm_item.name)

    #            ignored = True
    #            ignored_vm_name = ""
    #            for dup_vm in vm_in_db:
    #                if (
    #                    dup_vm.primary_ip4
    #                    and (vm_ip == str(dup_vm.primary_ip4.address.ip))
    #                    and vm_name == dup_vm.name
    #                ):
    #                    print(f"Matched VM {dup_vm.name}(IP: {vm_ip}) by primary_ip4 ")
    #                    count = update_inspur_vm(dup_vm, vm, ipaddr, vm_id)
    #                    updated_count += count
    #                    ignored = False
    #                    break
    #                elif vm_ip in dup_vm.description and vm_name == dup_vm.name:
    #                    print(f"Matched VM {dup_vm.name}(IP: {vm_ip}) by description")
    #                    count = update_inspur_vm(dup_vm, vm, ipaddr, vm_id)
    #                    updated_count += count
    #                    ignored = False
    #                    break

    #            if ignored:
    #                skipped.append(dup_vm.name)
    #        else:
    #            # Found 1 VM from netbox database, update
    #            vm_inst = vm_in_db.first()
    #            count = update_inspur_vm(vm_inst, vm, ipaddr, vm_id)
    #            updated_count += count
    #    else:
    #        print(f"Device with IP address({host_ip_address}) not found, skip")
    # except IPAddress.DoesNotExist:
    #    print(f"Device IP address not found {host_ip_address}, skip")
    #    continue
    # except KeyError as err:
    #    print(f"KeyError when processing {vm}, skip")
    #    continue
    # except IntegrityError as err:
    #    print(f"IntegrityError when processing {vm}, skip: {err}")
    #    # Use SQL below to check:
    #    # SELECT * FROM public.virtualization_virtualmachine where primary_ip4_id=<IPAddress ID>
    #    continue
    # ================ 以上为旧添加逻辑，后期优化后删除 ================


def update_inspur_vm(vm_inst: VirtualMachine, vm_data: dict) -> int:
    '''
    Return:
        1: updated
        0: no update
    '''
    try:
        vm_name = vm_data['name']
        vm_ip = vm_data['ip']

        # Logic to update vm instance in database
        # Update name
        if vm_inst.name != vm_name:
            print(
                f"🐛 Name of VM({vm_inst.name}: {vm_inst.description}[Database]) is different from VM({vm_name}: {vm_ip}[Cloud]), update"
            )
            vm_inst.name = vm_name

        # Update CPU/Mem
        vm_inst.vcpus = vm_data['vcpus']
        vm_inst.memory = vm_data['memory']
        # Update description
        vm_inst.description = vm_data.get('desc', '')

        # Update IP address in local_context_data
        if vm_ip:
            # Update IP
            if not vm_inst.local_context_data:
                vm_inst.local_context_data = {}
                vm_inst.local_context_data['_ip'] = vm_ip
                print(f"Add IP({vm_ip}) to local_context_data for VM({vm_name})")
            else:
                vm_inst.local_context_data['_ip'] = vm_ip
                print(f"update IP({vm_ip}) to local_context_data for VM({vm_name})")

        # Save to DB
        vm_inst.status = vm_data['status']
        vm_inst.save()

        return 1
    except Exception as err:
        return 0
