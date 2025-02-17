from django.core.management.base import BaseCommand, CommandError, CommandParser
from virtualization.utils.vmware import sync_vmware
from virtualization.utils.xen import sync_xen
from virtualization.utils.kvm import sync_kvm
from virtualization.utils.inspur import sync_inspur
from virtualization.utils.util import get_auth_from_comments, extract_ip
from virtualization.models import VirtualMachine
from virtualization.models import Cluster
from dcim.models import Device
from ipam.models import IPAddress



class Command(BaseCommand):
    help = '同步虚拟化群集中的虚拟机'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--list', action='store_true', dest='list', help='列出当前虚拟机群集')
        parser.add_argument('--clusters', nargs='+', dest='clusters', help='指定要同步的群集名')

    def print_msg(self, msg: str, level:str=""):
        if level == "success":
            self.stdout.write(self.style.SUCCESS(msg))
        elif level == "warning":
            self.stdout.write(self.style.WARNING(msg))
        elif level == "error":
            self.stdout.write(self.style.ERROR(msg))
        else:
            self.stdout.write(self.style.NOTICE(msg))

    def sync_cluster(self, clusters: list):
        for cluster in Cluster.objects.filter(name__in=clusters):
            cluster_type = cluster.type.name.lower()
            print(f"----cluster type: {cluster_type}")
            device_count = cluster.devices.count()
            self.print_msg(f"🐛 Found {device_count} devices in cluster({cluster})")

            if device_count == 0:
                self.print_msg(f"{cluster.name} 群集中未找到设备", "warning")
                continue

            
            # 浪潮云未找到可用API使用爬虫逻辑
            if cluster_type == 'inspur':
                vms = sync_inspur()
                self.print_msg(f"Found {len(vms)} VMs from Inspur cloud", "success")

                for vm in vms:
                    host_ip_address = f"{vm['host']}/24"
                    try:
                        ip_obj = IPAddress.objects.get(address=host_ip_address)
                        interface = ip_obj.assigned_object
    
                        # 检查接口是否存在并且是否关联到物理设备
                        if interface and hasattr(interface, 'device'):
                            device = interface.device

                            # 以下为浪潮虚拟机添加逻辑
                            # vm_in_db = VirtualMachine.objects.filter(cluster=cluster, name__contains=vm['name'])
                            vm_in_db = VirtualMachine.objects.filter(cluster=cluster, name=vm['name'])
                            if vm_in_db.count() == 0:
                                self.print_msg(f"VM({vm['name']}) not found in DB, adding")

                                vm_instance = VirtualMachine()
                                vm_instance.name = vm['name']
                                vm_instance.status = vm['status']

                                try:
                                    ip = vm['ip']
                                    # try:
                                    #     ipaddr, created = IPAddress.objects.get_or_create(address=ip)
                                    # except IPAddress.MultipleObjectsReturned:
                                    #     ipaddr = IPAddress.objects.filter(address=ip).first()
                                    # vm_instance.primary_ip4 = ipaddr

                                    # IP地址文本存放在描述里(临时)
                                    vm_instance.description = ip

                                except KeyError:
                                    self.print_msg(f"🐛 IP address for VM({vm['name']}) not found, skip", "warning")

                                if vm.get('vcpus', 0):
                                    vm_instance.vcpus = vm['vcpus']
                                if vm.get('memory', 0):
                                    vm_instance.memory = vm['memory']
                                vm_instance.cluster = cluster
                                # Bind vm to device(physical machine)
                                vm_instance.device = device
                                vm_instance.save()
                            else:
                                self.print_msg(f"🐛 Found {vm_in_db.count()} VMs in DB by search: {vm['name']}, skip", "warning")

                        else:
                            self.print_msg(f"🐛 Device with IP address({host_ip_address}) not found, skip", "warning")
                    except IPAddress.DoesNotExist:
                        self.print_msg(f"🐛 Device IP address not found {host_ip_address}, skip", "warning")
                        continue

                    # ipv4 = IPAddress()
                    # ipv4.address=f"{vm['host']}/24"
                    # print(ipv4)

                    # device = Device.objects.filter(primary_ip4=ipv4)
                    # print(f"----: {device}")
                # Sync VMS from Inspur Cloud
            else:
                for device in cluster.devices.all():
                    if not device.name:
                        self.print_msg(f"IP address not found in device name for device({device}) in cluster({cluster}), skip", "warning")
                        continue

                    device_ip = extract_ip(device.name)

                    # Get authentication from comments of cluster
                    # Format:
                    #   username: xxxxxx
                    #   password: yyyyyy
                    username = password = ""
                    username, password = get_auth_from_comments(device.comments)
                    if not username or not password:
                        self.print_msg(f" Username or password not found in comments of device({device}) in cluster({cluster}), skip", "error")
                        continue

                    self.print_msg(f"🐛 Sync VMs for device({device}) in cluster {cluster}")

                    if cluster_type == "vmware":
                        vms = sync_vmware(device_ip, username, password)
                    elif cluster_type == "xen":
                        vms = sync_xen(device_ip, username, password)
                    elif cluster_type == "kvm":
                        vms = sync_kvm(device_ip, username, password)

                    print(f"---vms: {vms}, {len(vms)}")
                    for vm in vms:
                        vm_in_db = VirtualMachine.objects.filter(cluster=cluster, name__contains=vm['name'])
                        if vm_in_db.count() == 0:
                            self.print_msg(f"VM({vm['name']} not found in DB, adding")

                            ip = extract_ip(vm['name'])

                            try:
                                ipaddr, created = IPAddress.objects.get_or_create(address=ip)
                                print(f"created: {created}")
                            except IPAddress.MultipleObjectsReturned:
                                ipaddr = IPAddress.objects.filter(address=ip).first()

                            vm_instance = VirtualMachine()
                            vm_instance.name = vm['name']
                            vm_instance.status = vm['status']
                            if not str(ipaddr).startswith("127.0.0.1"):
                                vm_instance.primary_ip4 = ipaddr
                            if vm.get('vcpus', 0):
                                vm_instance.vcpus = vm['vcpus']
                            if vm.get('memory', 0):
                                vm_instance.memory = vm['memory']
                            vm_instance.cluster = cluster
                            # Bind vm to device(physical machine)
                            vm_instance.device = device
                            vm_instance.save()
                        else:
                            self.print_msg(f"🐛 Found {vm_in_db.count()} VMs in DB by search: {vm['name']}, skip", "warning")

    def handle(self, *args, **options):
        # print(f"args: {args}")
        # print(f"options: {options}")
        # print(options.values())
        # print(any(options.values()))

        # 排除 verbosity，检查是否有自定义参数被提供
        custom_options = {k: v for k, v in options.items() if k not in ['verbosity', 'settings', 'pythonpath']}
        if not any(custom_options.values()):
            self.print_help('manage.py', 'sync_inspur')
            return


        if options["list"]:
            self.stdout.write(self.style.SUCCESS("Current Clusters:"))
            clusters = Cluster.objects.all()
            for cluster in clusters:
                print(f"{cluster}")
        elif options['clusters']:
            clusters = options['clusters']
            self.sync_cluster(clusters)
            # self.stdout.write(self.style.SUCCESS(f'Syncing data for clusters: {", ".join(clusters)}'))

        # self.stdout.write(self.style.SUCCESS("Done"))