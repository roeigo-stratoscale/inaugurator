from inaugurator import partitiontable
from inaugurator import targetdevice
from inaugurator import mount
from inaugurator import sh
from inaugurator import network
from inaugurator import loadkernel
from inaugurator import fstab
from inaugurator import passwd
from inaugurator import osmose
from inaugurator import osmosiscleanup
from inaugurator import talktoserver
from inaugurator import grub
from inaugurator import diskonkey
from inaugurator import cdrom
from inaugurator import udev
from inaugurator import download
from inaugurator import etclabelfile
from inaugurator import lvmetad
from inaugurator import verify
from inaugurator import debugthread
from inaugurator import storagedevices
from inaugurator import consts
from inaugurator import log
import os
import re
import time
import logging
import threading
import shutil


class Ceremony:
    def __init__(self, args):
        """
        args is a 'namespace' - an object, or maybe a bunch. The following members are required:
        inauguratorArgumentsSource - Indicates where the Inaugurator should read arguments from, apart from
                                     this argument; Either 'kernelCmdline' to read from kernel arguments or
                                     'processArguments' to read from the process arguments. Should this
                                     argument appear, it must appear as a process argument. The rest of the
                                     arguments should appear correspondingly to the value mentioned in this
                                     argument).
        inauguratorClearDisk - True will cause the disk to be erase even if partition layout is ok
        inauguratorSource - 'network', 'DOK' (Disk On Key), 'CDROM' or 'local' - select from where the label
                            should be osmosed. 'local' means the label is already in the local object
                            store, and is used in upgrades.
        inauguratorServerAMQPURL - the rabbitmq AMQP url to report status to. Can be 'None'. If used,
                                   the label itself is expected to come from a rabbitmq message.
        inauguratorMyIDForServer - the unique ID for this station, used for status reporting.
        inauguratorNetworkLabel - the label to use, in 'network' mode, if inauguratorServerAMQPURL was
                                  not specified
        inauguratorOsmosisObjectStores - the object store chain used when invoking osmosis (see osmosis
                                         documentation
        inauguratorChangeRootPassword - change the password in /etc/shadow to this
        inauguratorWithLocalObjectStore - use /var/lib/osmosis local object store as first tier in chain.
        inauguratorPassthrough - pass parameters to the kexeced kernel. Reminder: kexeced kernel are
                                 more vunerable to crashing, using this as the only channel of communication
                                 is risky
        inauguratorDownload - http get this file into a specific location, right before kexecing.
        inauguratorIgnoreDirs - ignore the following locations on disk, in the osmosis process. This is
                                usedful for upgrades - to keep the current configuration somewhere.
        inauguratorTargetDeviceCandidate - a list of devices (['/dev/vda', '/dev/sda']) to use as the
                                           inauguration target
        inauguratorStages - A comma-seperated list of stages to perform by order. Available stages:
                            'ceremony','kexec','reboot', 'shutdown'.
        inauguratorExpectedLabel - A label that identifies the source device, when using either a CDROM or
                                   a Diskonkey (in --inauguratorSource). If not used, then the first device
                                   of that kind that was found will be used.
        inauguratorIsNetworkAlreadyConfigured - If not given, and if inauguratorSource is 'network', then
                                                the network interface will be configured according
                                                to the following 4 arguments (that in which case, are
                                                manadtory).
        inauguratorUseNICWithMAC - use this specific NIC, with this specific MAC address
        inauguratorIPAddress - the IP address to configure to that NIC
        inauguratorExtraDataToGrubCmdLine - data to be added to the grub command line
        inauguratorNetmask
        inauguratorGateway
        """
        self._args = args
        self._talkToServer = None
        self._assertArgsSane()
        self._debugPort = None
        self._isExpectingReboot = False
        self._grubConfig = None
        self._before = time.time()
        self._bootPartitionPath = None
        self._wereDriversLoaded = False
        self._storageDevices = storagedevices.StorageDevices()
        self._fileHandler = None

    def ceremony(self):
        self._loadAllDriversIfNeeded()
        self._makeSureDiskIsMountable()
        with self._mountOp.mountRoot() as destination:
            self._copyInauguratorLog(destination)
            self._changeLogFileHandler(destination)
            self._initializeNetworkIfNeeded()
            self._disableNCQIfNeeded()
            self._readSmartDataIfNeeded()
            self._etcLabelFile = etclabelfile.EtcLabelFile(destination)
            self._doOsmosisFromSource(destination)
            logging.info("Osmosis complete")
            self._createBootAndInstallGrub(destination)
            logging.info("Boot sync complete")
            self._configureETC(destination)
            self._loadKernelForKexecing(destination)
            logging.info("kernel loaded")
            self._additionalDownload(destination)
            log.removeAllFileHandlers()

    def kexec(self):
        self._sync()
        self._verify()
        after = time.time()
        if self._talkToServer is not None:
            self._talkToServer.done()
        if self._before is not None:
            logging.info("Inaugurator took: %(interval).2fs.", dict(interval=after - self._before))
        logging.info("KEXECing...")
        self._loadKernel.execute()

    def reboot(self):
        self._sync()
        self._verify()
        sh.run("reboot -f")

    def shutdown(self):
        self._sync()
        self._verify()
        sh.run("poweroff -f")

    def _initializeNetworkIfNeeded(self):
        if self._args.inauguratorSource == 'network' and \
                not self._args.inauguratorIsNetworkAlreadyConfigured:
            network.Network(
                macAddress=self._args.inauguratorUseNICWithMAC, ipAddress=self._args.inauguratorIPAddress,
                netmask=self._args.inauguratorNetmask, gateway=self._args.inauguratorGateway)
            if self._args.inauguratorServerAMQPURL:
                self._talkToServer = talktoserver.TalkToServer(
                    amqpURL=self._args.inauguratorServerAMQPURL, myID=self._args.inauguratorMyIDForServer)

    def _assertArgsSane(self):
        logging.info("Command line arguments: %(args)s", dict(args=self._args))
        msg = "Unknown source for inaugurator: %s" % self._args.inauguratorSource
        assert self._args.inauguratorSource in ["network", "DOK", "local", "CDROM"], msg
        if self._args.inauguratorSource != "network":
            return
        if self._args.inauguratorServerAMQPURL is None and self._args.inauguratorNetworkLabel is None:
            msg = "If inauguratorSource is 'network', either inauguratorServerAMQPURL or " \
                  "inauguratorNetworkLabel must be specified."
            raise Exception(msg)
        if self._args.inauguratorOsmosisObjectStores is None:
            msg = "If inauguratorSource is 'network', the inauguratorOsmosisObjectStores argument must be " \
                  " specified."
            raise Exception(msg)
        if self._args.inauguratorIsNetworkAlreadyConfigured is None:
            mandatory = ["inauguratorUseNICWithMAC",
                         "inauguratorIPAddress",
                         "inauguratorNetmask",
                         "inauguratorGateway"]
            unspecified = [arg for arg in mandatory if getattr(self._args, arg) is None]
            if unspecified:
                msg = "If inauguratorIsNetworkAlreadyConfigured is not given, the following network " \
                      " command line arguments must be specified: %(mandatory)s. The following were not: " \
                      "%(unspecified)s" % \
                      dict(mandatory=", ".join(mandatory), unspecified=", ".join(unspecified))
                raise Exception(msg)
        if self._args.inauguratorServerAMQPURL is not None:
            assert self._args.inauguratorMyIDForServer is not None, \
                'If communicating with server, must specifiy --inauguratorMyIDForServer'
        if [bool(self._args.inauguratorTargetDeviceCandidate),
                bool(self._args.inauguratorTargetDeviceLabel),
                bool(self._args.inauguratorTargetDeviceType)].count(True) != 1:
            raise Exception("Invalid input arguments: inauguratorTargetDeviceCandidate, "
                            "inauguratorTargetDeviceLabel and inauguratorTargetDeviceType are "
                            "mutually exclusive")

    def _createPartitionTable(self):
        lvmetad.Lvmetad()
        logging.info("Requested root partition size: %(sizeGB)sGB",
                     dict(sizeGB=self._args.inauguratorRootPartitionSizeGB))
        partitionTable = partitiontable.PartitionTable(
            self._targetDevice,
            layoutScheme=self._args.inauguratorPartitionLayout,
            rootPartitionSizeGB=self._args.inauguratorRootPartitionSizeGB,
            bootPartitionSizeMB=self._args.inauguratorBootPartitionSizeMB,
            wipeOldInstallations=self._args.inauguratorWipeOldInauguratorInstallations)
        if self._args.inauguratorClearDisk:
            partitionTable.clear()
        partitionTable.verify()
        self._bootPartitionPath = partitionTable.getBootPartitionPath()

    def _configureETC(self, destination):
        self._etcLabelFile.write(self._label)
        fstab.createFSTab(
            rootPath=destination, root=self._mountOp.rootPartition(),
            boot=self._mountOp.bootPartition(), swap=self._mountOp.swapPartition())
        logging.info("/etc/fstab created")
        if self._args.inauguratorChangeRootPassword:
            passwd.setRootPassword(destination, self._args.inauguratorChangeRootPassword)
            logging.info("Changed root password")

    @staticmethod
    def _getSerialDevices():
        with open("/proc/cmdline", "r") as cmdLineFile:
            cmdLine = cmdLineFile.read()
        args = cmdLine.split(" ")
        keyValuePairs = [arg.split("=") for arg in args if arg.count("=") == 1]
        consoles = [value for key, value in keyValuePairs if key == "console"]
        return consoles

    def _createBootAndInstallGrub(self, destination):
        with self._mountOp.mountBoot() as bootDestination:
            sh.run("rsync -rlpgDS --delete-before %s/boot/ %s/" % (destination, bootDestination))
        with self._mountOp.mountBootInsideRoot():
            if self._args.inauguratorExtraDataToGrubCmdLine != "":
                grub.changeGrubConfiguration(destination, data=self._args.inauguratorExtraDataToGrubCmdLine)
            if "rhgb silent" not in self._args.inauguratorExtraDataToGrubCmdLine:
                serialDevices = self._getSerialDevices()
                if serialDevices:
                    logging.info("Overriding GRUB2 user settings to set serial devices to '%(devices)s'...",
                                 dict(devices=serialDevices))
                    serialDevicesStr = " ".join([dev for dev in serialDevices])
                    grub.changeGrubConfiguration(destination, data=serialDevicesStr, parameter="console")
                else:
                    logging.warn("a 'console' argument was not given. Cannot tell which serial device to "
                                 "redirect the console output to (default values in the label will be used)")
            else:
                logging.info("Removing all console parameters from command line")
                grub.changeGrubConfiguration(destination, data=None, parameter="console")
            logging.info("Installing GRUB2...")
            grubConfigPath = grub.install(self._targetDevice, destination)
            logging.info("Reading newly generated GRUB2 configuration file for later use...")
            grubConfigPathInDest = destination + grubConfigPath
            with open(grubConfigPathInDest, "r") as grubConfigFile:
                self._grubConfig = grubConfigFile.read()

    def _osmosFromNetwork(self, destination):
        self._debugPort = debugthread.DebugThread()
        if self._args.inauguratorServerAMQPURL:
            self._talkToServer.checkIn()
        try:
            osmos = osmose.Osmose(
                destination=destination,
                objectStores=self._args.inauguratorOsmosisObjectStores,
                withLocalObjectStore=self._args.inauguratorWithLocalObjectStore,
                noChainTouch=self._args.inauguratorNoChainTouch,
                ignoreDirs=self._args.inauguratorIgnoreDirs + [consts.INAUGURATOR_LOG_FILE_NAME],
                talkToServer=self._talkToServer)
            if self._args.inauguratorServerAMQPURL:
                self._label = self._talkToServer.label()
            else:
                self._label = self._args.inauguratorNetworkLabel
            osmos.tellLabel(self._label)
            osmos.wait()
        except Exception as e:
            if self._debugPort is not None and self._debugPort.wasRebootCalled():
                logging.info("Waiting to be reboot (from outside)...")
                blockForever = threading.Event()
                blockForever.wait()
            else:
                try:
                    self._talkToServer.failed(message=str(e))
                except:
                    pass
            raise e

    def _osmosFromDOK(self, destination):
        dok = diskonkey.DiskOnKey(self._args.inauguratorExpectedLabel)
        with dok.mount() as source:
            osmos = osmose.Osmose(
                destination, objectStores=source + "/osmosisobjectstore",
                withLocalObjectStore=self._args.inauguratorWithLocalObjectStore,
                noChainTouch=self._args.inauguratorNoChainTouch,
                ignoreDirs=self._args.inauguratorIgnoreDirs + [consts.INAUGURATOR_LOG_FILE_NAME],
                talkToServer=self._talkToServer)
            with open("%s/inaugurate_label.txt" % source) as f:
                self._label = f.read().strip()
            osmos.tellLabel(self._label)  # This must stay under the dok mount 'with' statement
            osmos.wait()

    def _osmosFromCDROM(self, destination):
        cdromInstance = cdrom.Cdrom(self._args.inauguratorExpectedLabel)
        with cdromInstance.mount() as source:
            osmos = osmose.Osmose(
                destination, objectStores=source + "/osmosisobjectstore",
                withLocalObjectStore=self._args.inauguratorWithLocalObjectStore,
                noChainTouch=self._args.inauguratorNoChainTouch,
                ignoreDirs=self._args.inauguratorIgnoreDirs + [consts.INAUGURATOR_LOG_FILE_NAME],
                talkToServer=self._talkToServer)
            with open("%s/inaugurate_label.txt" % source) as f:
                self._label = f.read().strip()
            osmos.tellLabel(self._label)  # This must stay under the mount 'with' statement
            osmos.wait()

    def _osmosFromLocalObjectStore(self, destination):
        osmos = osmose.Osmose(
            destination, objectStores=None,
            withLocalObjectStore=self._args.inauguratorWithLocalObjectStore,
            noChainTouch=self._args.inauguratorNoChainTouch,
            ignoreDirs=self._args.inauguratorIgnoreDirs + [consts.INAUGURATOR_LOG_FILE_NAME],
            talkToServer=self._talkToServer)
        self._label = self._args.inauguratorNetworkLabel
        osmos.tellLabel(self._label)
        osmos.wait()

    def _sync(self):
        logging.info("sync...")
        sh.run("busybox", "sync")
        logging.info("sync done")

    def _additionalDownload(self, destination):
        if self._args.inauguratorDownload:
            downloadInstance = download.Download(self._args.inauguratorDownload)
            downloadInstance.download(destination)

    def _copyInauguratorLog(self, dst):
        logging.info("Copying %s to %s", consts.INAUGURATOR_LOG_FILE_NAME, dst)
        dstLogFile = os.path.join(dst, consts.INAUGURATOR_LOG_FILE_NAME)
        if os.path.exists(dstLogFile):
            logging.info("Log file %s exists, removing it", dstLogFile)
            os.remove(dstLogFile)
        shutil.copyfile(consts.INAUGURATOR_RAM_LOG_FILE_NAME, dstLogFile)

    def _changeLogFileHandler(self, dst):
        log.removeAllFileHandlers()
        log.addFileHandler(os.path.join(dst, consts.INAUGURATOR_LOG_FILE_NAME))

    def _makeSureDiskIsMountable(self):
        self._setTargetDevice()
        self._createPartitionTable()
        logging.info("Partitions created")
        self._mountOp = mount.Mount()
        assert self._bootPartitionPath is not None, "Please initialize boot partition path first"
        self._mountOp.setBootPartitionPath(self._bootPartitionPath)

    def _setTargetDevice(self):
        if self._args.inauguratorTargetDeviceCandidate is not None:
            logging.info("Explicit target devices indicated: %(deviceCandidates)s",
                         dict(deviceCandidates=self._args.inauguratorTargetDeviceCandidate))
            candidates = self._args.inauguratorTargetDeviceCandidate
        elif self._args.inauguratorTargetDeviceType is not None:
            logging.info("Searching for target devices of type %(deviceType)s",
                         dict(deviceType=self._args.inauguratorTargetDeviceType))
            device = self._storageDevices.findFirstDeviceOfType(self._args.inauguratorTargetDeviceType,
                                                                self._talkToServer)
            candidates = [device]
        elif self._args.inauguratorTargetDeviceLabel is not None:
            logging.info("Searching for target devices with label %(deviceLabel)s",
                         dict(deviceLabel=self._args.inauguratorTargetDeviceLabel))
            candidates = self._getDevicesWithLabel(self._args.inauguratorTargetDeviceLabel)
        else:
            msg = ("Must specify at least one of the following arguments - "
                   "inauguratorTargetDeviceCandidate, inauguratorTargetDeviceType, "
                   "inauguratorTargetDeviceLabel")
            raise Exception(msg)
        logging.info("The following devices are the candidates for inauguration %s" % candidates)
        if len(candidates) > 1:
            raise Exception(("Cannot have more than 1 device as candidate, candidates %s" %
                             candidates))
        self._targetDevice = targetdevice.TargetDevice.device(candidates)

    def _getDevicesWithLabel(self, label):
        RETRIES = 5
        for retry in xrange(RETRIES):
            candidates = list(partitiontable.PartitionTable.getDevicesWithLabel(label))
            if candidates:
                break
            else:
                logging.info("No devices found with label %s, retry %s" % (label, retry))
        else:
            raise Exception("No devices found with label %s" % label)
        logging.info("Found the following devices wuth label %s - %s" % (label, candidates))
        return partitiontable.PartitionTable.getOriginDevices(candidates)

    def _loadKernelForKexecing(self, destination):
        self._loadKernel = loadkernel.LoadKernel()
        self._loadKernel.fromBootPartitionGrubConfig(
            grubConfig=self._grubConfig,
            bootPath=os.path.join(destination, "boot"), rootPartition=self._mountOp.rootPartition(),
            append=self._args.inauguratorPassthrough)

    def _doOsmosisFromSource(self, destination):
        if self._args.inauguratorWipeOsmosisObjectStoreIfNeeded:
            osmosiscleanup.OsmosisCleanup(
                destination,
                usageUpperThreshold=self._args.inauguratorCleanupUpperPercentageThreshold)
        if self._args.inauguratorSource == 'network':
            self._osmosFromNetwork(destination)
        elif self._args.inauguratorSource == 'DOK':
            self._osmosFromDOK(destination)
        elif self._args.inauguratorSource == 'CDROM':
            self._osmosFromCDROM(destination)
        elif self._args.inauguratorSource == 'local':
            self._osmosFromLocalObjectStore(destination)
        else:
            assert False, "Unknown source %s" % self._args.inauguratorSource

    def _verify(self):
        if not self._args.inauguratorVerify:
            return
        self._sync()
        verify.Verify.dropCaches()
        with self._mountOp.mountRoot() as destination:
            verify.Verify(destination, self._label, self._talkToServer, self._localObjectStore).go()

    def _loadAllDriversIfNeeded(self):
        if not self._wereDriversLoaded:
            udev.loadAllDrivers()
            self._wereDriversLoaded = True

    def _disableNCQIfNeeded(self):
        if self._args.inauguratorDisableNCQ:
            self._storageDevices.disableNCQ()
        else:
            logging.info('Skipping the disabling of NCQ.')

    def _readSmartDataIfNeeded(self):
        if not self._args.inauguratorDontReadSmartData:
            self._storageDevices.readSmartDataFromAllDevices(
                talkToServer=self._talkToServer,
                failOnFailedHealthTest=not self._args.inauguratorDontFailOnFailedDisk)
