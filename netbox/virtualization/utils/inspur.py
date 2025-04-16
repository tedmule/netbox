import os
import urllib3
import requests


def sync_inspur():
    inspur_cloud_url = os.getenv('INSPUR_CLOUD_URL', 'https://localhost')
    inspur_cloud_access_key = os.getenv('INSPUR_CLOUD_ACCESS_KEY', 'access_key')
    inspur_cloud_access_secret = os.getenv('INSPUR_CLOUD_ACCESS_SECRET', 'access_secret')
    inspur_cloud_host = os.getenv('INSPUR_CLOUD_HOST', '')

    headers = {
        # 'Cookie': os.getenv('INSPUR_COOKIE', ''),
        'version': "5.8",
        'Cache-Control': 'no-cache',
        'Authorization': f'ICS {inspur_cloud_access_key}:{inspur_cloud_access_secret}'
    }

    if inspur_cloud_host:
        headers['Host'] = inspur_cloud_host

    vms = []
    urllib3.disable_warnings()
    # resp = requests.get('https://192.168.20.35/vms?pageSize=1000&currentPage=1&sortField=&sort=desc',
    resp = requests.get(f'{inspur_cloud_url}/vms?pageSize=1000&currentPage=1&sortField=&sort=desc',
                         headers=headers, verify=False)
    if resp.status_code == 200:
        data = resp.json()
        for vm in data['items']:
            info = {
                'name': vm['name'],
                'status': 'active' if vm['status'] == "STARTED" else "offline",
                'vcpus': vm['cpuNum'],
                'memory': vm['memory'],
                'host': vm['hostIp'],
            }

            if len(vm['nics']) > 0:
                ip_text = vm['nics'][0]['ip']

                if ip_text:
                    info['ip'] = ip_text.split(',')[0]
                else:
                    print(f"🐛IP address not found for VM {vm['name']}, skip")
                    continue
            else:
                print(f"🐛NIC not found for VM {vm['name']}, skip")
                continue

            vms.append(info)
        return vms
    else:
        print(f"🐛Invoke Inspur API error: status({resp.status_code}), response({resp.text})")
        return []