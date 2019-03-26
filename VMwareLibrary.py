#!/usr/bin/python3
""" Robot Framework VMWare library using pyVmomi """

import atexit
import logging
import time
import uuid

import requests
from pyVim.connect import Disconnect, SmartConnectNoSSL
from pyVmomi import vim, vmodl  # pylint: disable=E0611
from robot.api import logger
from tenacity import (
    retry,
    retry_if_not_result,
    RetryError,
    stop_after_delay,
    wait_fixed,
)


class VMwareLibrary:

    def __init__(self, host, port, user, password):
        # List of properties.
        # See: http://goo.gl/fjTEpW
        # for all properties.
        #
        # Some options: ['name', 'network', 'config.uuid', 'runtime.powerState',
        #  'config.hardware.numCPU','config.hardware.memoryMB', 'guest.guestState',
        # 'config.guestFullName', 'config.guestId','config.version']

        # We do the connection here
        try:
            service_instance = SmartConnectNoSSL(host=host,
                                                 user=user,
                                                 pwd=password,
                                                 port=port)
            atexit.register(Disconnect, service_instance)

        except ConnectionError:
            logger.info('Could not connect, check the status of the vmware server.')
            return

        # Note: collector is a tuple (collector, view_ref)
        filter_spec = self.__get_filter_spec(service_instance)

        self.content = service_instance.RetrieveContent()
        self.pm = self.content.guestOperationsManager.processManager

        self._filter = filter_spec
        self._si = service_instance
        self._user = user
        self._password = password

    def is_vm_on(self, vm_name):
        """ Get the power state of a particular VM """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        if vm['powerState'] == 'poweredOn':
            return True
        else:
            return False

    def is_vm_off(self, vm_name):
        """ Get the power state of a particular VM """
        return not self.is_vm_on(vm_name)

    # def get_vm_alarms(self):
    #     INDEX = self._si.content.searchIndex
    #     alarm_list = []
    #     if INDEX:
    #         HOST = INDEX.FindByUuid(datacenter=None, uuid=MY_ARGS.uuid, vmSearch=False)
    #         alarm_list.append(HOST)

    def test_vm_ip_access(self, vm_name):
        """ Check the IP access to a VM, fails if none """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        output = {x.network: x.ipAddress for x in vm_obj.guest.net}

        if not bool(output):
            raise Exception('VM not available')
        else:
            return 0

    def wait_for_vm(self, vm_name):
        """ Check the IP access to a VM, fails if none """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        output = {x.network: x.ipAddress for x in vm_obj.guest.net}

        start_time = time.time()
        end_time = start_time
        while (not bool(output)) and ((end_time - start_time) < 240):  # Timeout 4 minutes
            output = {x.network: x.ipAddress for x in vm_obj.guest.net}
            time.sleep(5)
            end_time = time.time()
        if not bool(output):
            return 'Timed out waiting for connection'
        else:
            return 'Success'

    def get_vm_ip(self, vm_name, network_name):
        # Get the IP address of a running VM with ope vm tools installed
        """ Get the reachable IP Address of the VM """

        @retry(
            retry=retry_if_not_result(lambda n: n),
            wait=wait_fixed(2),
            stop=stop_after_delay(30))
        def wait_for_data():
            vms = self.__get_data(self._si, self._filter)
            vm = vms[vm_name]
            vm_obj = vm['obj']
            output = [x.ipAddress for x in vm_obj.guest.net if x.network == network_name]
            return output

        try:
            ip_address = wait_for_data()
            return ip_address[0][0]
        except RetryError:
            return 'not reported'

    def get_vm_network_name(self, vm_name):
        # Get a list of network device names of a running VM with open vm tools installed
        """ Get the reachable IP Address of the VM """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        network_name = [x.network for x in vm_obj.guest.net]

        if not network_name:
            return 'not reported'
        else:
            return network_name

    def get_vm_mac_addresses(self, vm_name, network_name):
        # Get the MAC address of a running VM with ope vm tools installed
        """ Get the reachable IP Address of the VM """

        @retry(
            retry=retry_if_not_result(lambda n: n),
            wait=wait_fixed(2),
            stop=stop_after_delay(30))
        def wait_for_data():
            vms = self.__get_data(self._si, self._filter)
            vm = vms[vm_name]
            vm_obj = vm['obj']
            mac_address = [x.macAddress for x in vm_obj.guest.net if x.network == network_name]
            return mac_address

        try:
            mac_address = wait_for_data()
            return mac_address[0]
        except RetryError:
            return 'not reported'

    def update_virtual_nic_state(self, vm_name, interface_label, new_nic_state):
        """
        :param vm_name: Virtual Machine Name
        :param interface_name: Network Interface Label
        :param new_nic_state: connect, disconnect or delete
        :return: True if success
        """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        virtual_nic_device = None

        for dev in vm_obj.config.hardware.device:
            logging.debug(dev.deviceInfo.label)
            if isinstance(dev, vim.vm.device.VirtualEthernetCard) \
                    and dev.deviceInfo.label == interface_label:
                virtual_nic_device = dev
        if not virtual_nic_device:
            raise RuntimeError('Virtual {} could not be found.'.format(interface_label))

        virtual_nic_spec = vim.vm.device.VirtualDeviceSpec()
        virtual_nic_spec.operation = \
            vim.vm.device.VirtualDeviceSpec.Operation.remove \
            if new_nic_state == 'delete' \
            else vim.vm.device.VirtualDeviceSpec.Operation.edit
        virtual_nic_spec.device = virtual_nic_device
        virtual_nic_spec.device.key = virtual_nic_device.key
        virtual_nic_spec.device.macAddress = virtual_nic_device.macAddress
        virtual_nic_spec.device.backing = virtual_nic_device.backing
        virtual_nic_spec.device.wakeOnLanEnabled = \
            virtual_nic_device.wakeOnLanEnabled
        connectable = vim.vm.device.VirtualDevice.ConnectInfo()
        if new_nic_state == 'connect':
            connectable.connected = True
            connectable.startConnected = True
        elif new_nic_state == 'disconnect':
            connectable.connected = False
            connectable.startConnected = False
        else:
            connectable = virtual_nic_device.connectable
        virtual_nic_spec.device.connectable = connectable
        dev_changes = []
        dev_changes.append(virtual_nic_spec)
        spec = vim.vm.ConfigSpec()
        spec.deviceChange = dev_changes
        task = vm_obj.ReconfigVM_Task(spec=spec)
        self.__wait_for_task(task)
        return True

    def rename_vm(self, old_vm_name, new_vm_name):
        # Change old_vm_name to new_vm_name
        """ Get the reachable IP Address of the VM """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[old_vm_name]
        vm_obj = vm['obj']
        task = vm_obj.Rename(new_vm_name)
        self.__wait_for_task(task)

    def verify_vm_exists(self, vm_name):
        # Checks if VM exist by name and fail if not
        """ Check for the VM name """
        vms = self.__get_data(self._si, self._filter)
        if vm_name in vms:
            return True
        else:
            raise Exception(vm_name + ' not available')

    def check_vm_exists(self, vm_name):
        # Checks if VM exist by name and return boolian
        """ Check for the VM name """
        vms = self.__get_data(self._si, self._filter)
        if vm_name in vms:
            return True
        else:
            return False

    def power_off_vm(self, vm_name, timeout=15):
        # Power off a VM by name
        """ Power off a VM by name """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        vm_obj.ShutdownGuest()
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            if self.is_vm_off(vm_name):
                break
            time.sleep(1)
        else:
            task = vm_obj.PowerOff()
            self.__wait_for_task(task)

    def power_on_vm(self, vm_name):
        # Powers on a VM by name
        """ Powers on a VM by name """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        task = vm_obj.PowerOn()
        self.__wait_for_task(task)

    def standby_vm(self, vm_name):
        # Puts VM in standby
        """ Powers on a VM by name """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        task = vm_obj.StandbyGuest()
        self.__wait_for_task(task)

    def reset_vm(self, vm_name):
        # Reset the VM by name - note: VM needs to be powered on
        """ Reset the VM by name - note: VM needs to be powered on """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        task = vm_obj.Reset()
        self.__wait_for_task(task)

    def create_snapshot_with_memory(self, vm_name, snapshot_name):
        # Create a snapshot with memory and a given name using VM name to identify
        """ Create a snapshot with memory and a given name using VM name to identify """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        task = vm_obj.CreateSnapshot(snapshot_name, 'Created by robot', True, False)
        self.__wait_for_task(task)

    def create_snapshot_without_memory(self, vm_name, snapshot_name):
        # Create a snapshot without memory and a given name using VM name to identify
        """ Create a snapshot without memory and a given name using VM name to identify """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        task = vm_obj.CreateSnapshot(snapshot_name, 'Created by Robot', False, False)
        self.__wait_for_task(task)

    def revert_to_current_snapshot(self, vm_name):
        # Revert to the last snapshot created
        """ Revert to the last snapshot created """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        task = vm_obj.RevertToCurrentSnapshot()
        self.__wait_for_task(task)

    def get_snapshot(self, snapshot_name, snapshot_list):
        for snapshot in snapshot_list:
            if snapshot.name == snapshot_name:
                return snapshot.snapshot
            if snapshot.childSnapshotList:
                return self.get_snapshot(snapshot_name, snapshot.childSnapshotList)

    def revert_to_snapshot(self, vm_name, snapshot_name):
        return self.revert_to_specific_snapshot(vm_name, snapshot_name, fail_if_missing=True)

    def revert_to_specific_snapshot(self, vm_name, snapshot_name, fail_if_missing=False):
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        snapshot = None
        if vm_obj.snapshot:
            snapshot = self.get_snapshot(snapshot_name, vm_obj.snapshot.rootSnapshotList)
        if snapshot is not None:
            task = snapshot.RevertToSnapshot_Task()
            self.__wait_for_task(task)
        elif fail_if_missing:
            raise RuntimeError(f'No such snapshot {snapshot_name} on {vm_name}')

    def remove_specific_snapshot(self, vm_name, snapshot_name, fail_if_missing=False):
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        snapshot = None
        if vm_obj.snapshot:
            snapshot = self.get_snapshot(snapshot_name, vm_obj.snapshot.rootSnapshotList)
        if snapshot is not None:
            task = snapshot.RemoveSnapshot_Task()
            self.__wait_for_task(task)
        elif fail_if_missing:
            raise RuntimeError(f'No such snapshot {snapshot_name} on {vm_name}')

    def update_specific_snapshot(self, vm_name, snapshot_name, with_memory=False):
        self.remove_specific_snapshot(vm_name, snapshot_name)
        if with_memory:
            self.create_snapshot_with_memory(vm_name, snapshot_name)
        else:
            self.create_snapshot_without_memory(vm_name, snapshot_name)

    def remove_all_snapshots(self, vm_name):
        # Deletes all snapshots
        """ Removes all snapshots """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        task = vm_obj.RemoveAllSnapshots()
        self.__wait_for_task(task)

    def get_vm__overall_status(self, vm_name):
        # Gets status of VM, i.e 'green'
        """ Gets status of VM, i.e 'green' """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        return vm_obj.overallStatus

    def verify_vmware_tools_are_available(self, vm_name):
        """ Checks if VMWareTools is available, does not check it is running """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['toolsStatus']
        if vm_obj == 'toolsOk':
            return True
        else:
            raise Exception(vm_name + ' VMware Tools not available')

    def verify_vmware_tools_are_running(self, vm_name):
        """ Checks if VMWareTools is actually running """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['toolsRunningStatus']
        if vm_obj == 'guestToolsRunning':
            return True
        else:
            raise Exception(vm_name + ' VMware Tools not running')

    def vm_screen(self, vm_name):
        """ Returns the screen resolution of the VM """
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['screen']
        return vm_obj  # To do: make this more useful by extracting width and height as tuple

    def set_screen_resolution(self, vm_name, width, height):
        """ Sets the screen resolution of the VM - not working currently """
        # This currently comes back with un-supported
        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        rc = vm_obj.SetScreenResolution(int(width), int(height))
        return rc

    def run_program_with_fail(self, vm_name, vm_username, vm_password,
                              path_to_program, program_arguments='', timeout=60):
        ''' Run a program and raises exception if it fails
        - Primary usecase for use with "Wait until keyword succeeds"
        - returns the stdout
        '''
        return_value = self.run_program(vm_name, vm_username, vm_password,
                                        path_to_program, program_arguments, timeout)
        if return_value['exit_code'] != 0:
            raise RuntimeError('Are we there yet?')
        else:
            return return_value['stdout']

    def run_program(self, vm_name, vm_username, vm_password,
                    path_to_program, program_arguments='', timeout=60):
        ''' Run a program on the VM and return a dict containing:
                    owner
                    start_time
                    end_time
                    exit_code
                    name          Name of execuitable
                    cmd_line      Full path to execuitable plus arguments
                    stdout
        '''
        service_instance = self._si
        vms = self.__get_data(service_instance, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']

        # Note I was getting the following error if I didn't use keyword arguments with
        # NamePasswordAuthentication (I.E used positional arguments):
        #
        # TypeError: __init__() takes 1 positional argument but 3 were given
        creds = vim.vm.guest.NamePasswordAuthentication(username=vm_username, password=vm_password)
        tempfilename = f'/tmp/{uuid.uuid4()}.txt'

        ps = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath=path_to_program,
            arguments=f'{program_arguments} &> {tempfilename}',
        )

        res = self.pm.StartProgramInGuest(vm=vm_obj, auth=creds, spec=ps)

        res_data = self.wait_for_process(vm_obj, res, creds, timeout)
        if not res_data:
            raise Exception(f'Timed out running program {path_to_program}')

        results = dict(uuid=vm_obj.summary.config.uuid,
                       owner=res_data.owner,
                       start_time=res_data.startTime.isoformat(),
                       end_time=res_data.endTime.isoformat(),
                       exit_code=res_data.exitCode,
                       name=res_data.name,
                       cmd_line=res_data.cmdLine)

        if res_data.exitCode != 0:
            results['msg'] = 'Failed to execute command'
            results['changed'] = False
            results['failed'] = True
        else:
            results['changed'] = True
            results['failed'] = False

        url_info = self.content.guestOperationsManager.fileManager.InitiateFileTransferFromGuest(
            vm_obj, creds, tempfilename)

        logging.debug('DOWNLOAD OUTPUT FROM: ' + url_info.url)
        return_info = requests.get(url_info.url, verify=False)
        ConsoleOut = return_info.text
        logging.debug('Return data: ' + ConsoleOut)

        self.content.guestOperationsManager.fileManager.DeleteFileInGuest(
            vm_obj, creds, tempfilename)

        results['stdout'] = ConsoleOut

        return results

    def process_exists_in_guest(self, vm, pid, creds):
        res = self.pm.ListProcessesInGuest(vm, creds, pids=[pid])
        if not res:
            return False
        res = res[0]
        if res.exitCode is None:
            return True, ''
        elif res.exitCode >= 0:
            return False, res
        else:
            return True, res

    def wait_for_process(self, vm, pid, creds, timeout):
        start_time = time.time()
        while True:
            current_time = time.time()
            process_status, res_data = self.process_exists_in_guest(vm, pid, creds)
            if not process_status:
                return res_data
            elif current_time - start_time >= timeout:
                break
            else:
                time.sleep(5)

    def list_running_programs(self, vm_name, vm_username, vm_password):
        """ Returns a list of running processes on the VM """
        service_instance = self._si
        vms = self.__get_data(service_instance, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        creds = vim.vm.guest.NamePasswordAuthentication(username=vm_username, password=vm_password)
        pids = self.pm.ListProcessesInGuest(vm_obj, creds)
        return pids

    def test_vm_process_is_running(self, vm_name, vm_username, vm_password, pid):
        """ Checks if program is running and returns True / False """
        service_instance = self._si
        vms = self.__get_data(service_instance, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        creds = vim.vm.guest.NamePasswordAuthentication(username=vm_username, password=vm_password)
        pid_data = self.pm.ListProcessesInGuest(vm_obj, creds, [pid])
        exit_code = pid_data[0].exitCode
        if exit_code is None:
            return True
        else:
            return False

    def get_vm_process_data(self, vm_name, vm_username, vm_password, pid):
        """ Gets process data and returns dictionary """
        service_instance = self._si
        vms = self.__get_data(service_instance, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        creds = vim.vm.guest.NamePasswordAuthentication(username=vm_username, password=vm_password)
        pid_data = self.pm.ListProcessesInGuest(vm_obj, creds, [pid])
        return {'name': pid_data[0].name, 'owner': pid_data[0].owner,
                'cmdLine': pid_data[0].cmdLine, 'startTime': pid_data[0].startTime,
                'endTime': pid_data[0].endTime, 'exitCode': pid_data[0].exitCode}

    def verify_vm_process_is_stopped(self, vm_name, vm_username, vm_password, pid):
        """ Checks if program is running and fails """
        service_instance = self._si
        vms = self.__get_data(service_instance, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        creds = vim.vm.guest.NamePasswordAuthentication(username=vm_username, password=vm_password)
        pid_data = self.pm.ListProcessesInGuest(vm_obj, creds, [pid])
        exit_code = pid_data[0].exitCode
        if exit_code is None:
            raise Exception('Process ' + str(pid) + ' is running')
        else:
            return True

    def clone_virtual_machine(self, vm_name, clone_vm_name, power_on=True):
        """ Clone a VM from vm_name,  power_on is optional. """

        vms = self.__get_data(self._si, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']

        template = self.__get_obj(self.content, [vim.VirtualMachine], None)

        # if none get the first one
        datacenter = self.__get_obj(self.content, [vim.Datacenter], None)

        destfolder = datacenter.vmFolder

        datastore = self.__get_obj(self.content, [vim.Datastore],
                                   template.datastore[0].info.name)

        # if None, get the first one
        cluster = self.__get_obj(self.content, [vim.ClusterComputeResource], None)

        resource_pool = cluster.resourcePool

        vmconf = vim.vm.ConfigSpec()

        podsel = vim.storageDrs.PodSelectionSpec()
        pod = self.__get_obj(self.content, [vim.StoragePod], None)
        podsel.storagePod = pod

        storagespec = vim.storageDrs.StoragePlacementSpec()
        storagespec.podSelectionSpec = podsel
        storagespec.type = 'create'
        storagespec.folder = destfolder
        storagespec.resourcePool = resource_pool
        storagespec.configSpec = vmconf

        try:
            rec = self.content.storageResourceManager.RecommendDatastores(
                storageSpec=storagespec)
            rec_action = rec.recommendations[0].action[0]
            real_datastore_name = rec_action.destination.name
        except Exception:  # Yes I'm aware this is bad
            real_datastore_name = template.datastore[0].info.name

        datastore = self.__get_obj(self.content, [vim.Datastore], real_datastore_name)

        # set relospec
        relospec = vim.vm.RelocateSpec()
        relospec.datastore = datastore
        relospec.pool = resource_pool

        clonespec = vim.vm.CloneSpec()
        clonespec.location = relospec
        clonespec.powerOn = power_on

        logger.info('cloning VM...')
        task = vm_obj.Clone(folder=destfolder, name=clone_vm_name, spec=clonespec)
        self.__wait_for_task(task)

    def destroy_virtual_machine(self, vm_name):
        service_instance = self._si

        VM = self.__get_obj(service_instance.content, [vim.VirtualMachine], vm_name)

        if VM is None:
            return

        logger.info(f'Found: {VM.name}')
        logger.info(f'The current powerState is: {VM.runtime.powerState}')
        if format(VM.runtime.powerState) == "poweredOn":
            logger.info(f'Attempting to power off {VM.name}')
            task = VM.PowerOffVM_Task()
            self.__wait_for_task(task)
            logger.info(f'{task.info.state}')

        logger.info('Destroying VM from vSphere.')
        task = VM.Destroy_Task()
        self.__wait_for_task(task)
        logger.info('Done.')

    def connect_virtual_machine_network_adapter_to(
            self, vm_name, network_adapter_label, new_network_name):
        """
        Connect a specific virtual machine's network adapter to a
        named virtual network.

        For example:

        >>> connect_virtual_machine_network_adapter_to(
        ...     'nathan-solo-machine', 'Network adapter 2',
        ...     'DHCP Testing Local network')

        will connect the "nathan-solo-machine" VM's network adapter
        "Network adapter 2" to the "DHCP Testing Local network"
        virtual network.
        """
        vm = self.__get_vm_by_name(vm_name)
        network_change = self.create_network_change(
            self.__get_vm_network_adapter_by_label(vm, network_adapter_label),
            self.__get_network_by_name(new_network_name))

        task = self.create_task_to_reconfigure_vm(vm, network_change)
        self.__wait_for_task(task)
        return task.info

    def create_task_to_reconfigure_vm(self, virtual_machine, *config_changes):
        """
        Create a VMware task to reconfigure a virtual machine with the
        given changes.
        """
        config_spec = vim.vm.ConfigSpec()
        config_spec.deviceChange = list(config_changes)
        return virtual_machine.ReconfigVM_Task(spec=config_spec)

    def create_network_change(self, network_adapter, new_network):
        """
        Create a VMware API "device change spec" to change the network
        an adapter is connected to.
        """
        # To reconfigure a VM's network adapter, you must construct a
        # VMware API object called a "virtual device spec", which
        # describes the desired new state of the device.
        change_spec = vim.vm.device.VirtualDeviceSpec()
        change_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit

        # These fields aren't being changed, but we still need to copy
        # them from the previous device state.
        change_spec.device = network_adapter
        change_spec.device.key = network_adapter.key
        change_spec.device.macAddress = network_adapter.macAddress
        change_spec.device.wakeOnLanEnabled = network_adapter.wakeOnLanEnabled
        # This is written with the assumption we want the network
        # adapter to actually be connected to the new network.
        connectable = vim.vm.device.VirtualDevice.ConnectInfo()
        connectable.connected = True
        connectable.startConnected = True
        change_spec.device.connectable = connectable

        # This is the actual change in configuration: we change the
        # network the adapter is connected to by changing the "backing"
        # of the device.
        change_spec.device.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        change_spec.device.backing.network = new_network
        change_spec.device.backing.deviceName = new_network.name

        return change_spec

    def test_service_instance(self, vm_name):
        """ WTF """
        service_instance = self._si
        vms = self.__get_data(service_instance, self._filter)
        vm = vms[vm_name]
        vm_obj = vm['obj']
        return vm_obj.Datacenter

    ##############################################################
    # Private functions
    ##############################################################
    def __get_vm_by_name(self, vm_name):
        try:
            return self.__get_obj(
                self._si.RetrieveContent(), [vim.VirtualMachine], vm_name)
        except IndexError as err:
            raise Exception(f"Couldn't find virtual machine named {vm_name!r}") from err

    def __get_vm_network_adapter_by_label(self, vm, network_adapter_label):
        try:
            return [
                device for device in vm.config.hardware.device
                if isinstance(device, vim.vm.device.VirtualEthernetCard) and
                device.deviceInfo.label == network_adapter_label
            ][0]
        except IndexError as err:
            raise Exception(
                f"Couldn't find network adapter labelled {network_adapter_label!r} "
                f"on virtual machine {vm.name!r}") from err

    def __get_network_by_name(self, network_name):
        try:
            return self.__get_obj(
                self._si.RetrieveContent(), [vim.Network], network_name)
        except IndexError as err:
            raise Exception(f"Couldn't find network named {network_name!r}") from err

    def __get_filter_spec(self, service_instance):

        container = service_instance.content.rootFolder

        view_ref = service_instance.content.viewManager.CreateContainerView(
            container=container,
            type=[vim.VirtualMachine],
            recursive=True,
        )

        path_set = ['name', 'network', 'runtime.powerState', 'guest.guestState',
                    'guest.toolsStatus', 'guest.toolsRunningStatus',
                    'guest.screen', 'config.guestId']
        # Create object specification to define the starting point of
        # inventory navigation
        obj_spec = vmodl.query.PropertyCollector.ObjectSpec()
        obj_spec.obj = view_ref
        obj_spec.skip = True

        # Create a traversal specification to identify the path for collection
        traversal_spec = vmodl.query.PropertyCollector.TraversalSpec()
        traversal_spec.name = 'traverseEntities'
        traversal_spec.path = 'view'
        traversal_spec.skip = False
        traversal_spec.type = view_ref.__class__
        obj_spec.selectSet = [traversal_spec]

        # Identify the properties to the retrieved
        property_spec = vmodl.query.PropertyCollector.PropertySpec()
        property_spec.type = vim.VirtualMachine

        if not path_set:
            property_spec.all = True

        property_spec.pathSet = path_set

        # Add the object and property specification to the
        # property filter specification
        filter_spec = vmodl.query.PropertyCollector.FilterSpec()
        filter_spec.objectSet = [obj_spec]
        filter_spec.propSet = [property_spec]
        return filter_spec

    def __get_data(self, service_instance, filter_spec):

        waitopts = vmodl.query.PropertyCollector.WaitOptions()
        waitopts.maxWaitSeconds = 0

        collector = service_instance.content.propertyCollector

        update_result = collector.WaitForUpdatesEx('', waitopts)
        # Retrieve properties
        while update_result:
            update_result = collector.WaitForUpdatesEx(update_result, waitopts)
            logger.info(update_result)
        property_collector = collector.RetrieveContents([filter_spec])
        vm_view = []
        for obj in property_collector:
            properties = {}
            for prop in obj.propSet:
                properties[prop.name] = prop.val
                properties['obj'] = obj.obj
            vm_view.append(properties)

        vms = [vm for vm in vm_view]

        vms_dict = {vm['name']: {
            'guestId': vm.get('config.guestId', None),
            'guestState': vm.get('guest.guestState', None),
            'network': vm.get('network', None),
            'powerState': vm.get('runtime.powerState', None),
            'toolsStatus': vm.get('guest.toolsStatus', None),
            'toolsRunningStatus': vm.get('guest.toolsRunningStatus', None),
            'screen': vm.get('guest.screen', None),
            'obj': vm.get('obj', None),
        } for vm in vms}

        return vms_dict

    def __wait_for_task(self, task):
        """ wait for a vCenter task to finish """
        task_done = False
        while not task_done:
            if task.info.state == 'success':
                return task.info.result

            if task.info.state == 'error':
                logger.info('there was an error')
                task_done = True

    def __get_obj(self, content, vimtype, name):
        """
        Return an object by name, if name is None the
        first found object is returned
        """
        obj = None
        container = content.viewManager.CreateContainerView(
            content.rootFolder, vimtype, True)
        for c in container.view:
            if name:
                if c.name == name:
                    obj = c
                    break
            else:
                obj = c
                break

        return obj
