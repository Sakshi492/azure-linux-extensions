#!/usr/bin/env python
#
# OmsAgentForLinux Extension
#
# Copyright 2015 Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import os.path
import re
import sys
import traceback
import time
import platform
import subprocess
import json
import base64
import inspect

try:
    from Utils.WAAgentUtil import waagent
    import Utils.HandlerUtil as HUtil
except Exception as e:
    # These utils have checks around the use of them; this is not an exit case
    print('Importing utils failed with error: {0}'.format(e))

# Global Variables
PackagesDirectory = 'packages'
BundleFileName = 'omsagent-1.4.1-45.universal.x64.sh'
GUIDRegex = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
GUIDOnlyRegex = r'^' + GUIDRegex + '$'
SCOMCertIssuerRegex = r'^[\s]*Issuer:[\s]*CN=SCX-Certificate/title=SCX' + GUIDRegex + ', DC=.*$'
SCOMPort = 1270

# Paths
OMSAdminPath = '/opt/microsoft/omsagent/bin/omsadmin.sh'
OMSAgentServiceScript = '/opt/microsoft/omsagent/bin/service_control'
OMIConfigEditorPath = '/opt/omi/bin/omiconfigeditor'
OMIServerConfPath = '/etc/opt/omi/conf/omiserver.conf'
EtcOMSAgentPath = '/etc/opt/microsoft/omsagent/'
SCOMCertPath = '/etc/opt/microsoft/scx/ssl/scx.pem'

# Commands
# Always use upgrade - will handle install if scx, omi are not installed or
# upgrade if they are
InstallCommandTemplate = '{0} --upgrade'
UninstallCommandTemplate = '{0} --remove'
WorkspaceCheckCommand = '{0} -l'.format(OMSAdminPath)
OnboardCommandWithOptionalParamsTemplate = '{0} -d opinsights.azure.us -w {1} -s {2} {3}'
RestartOMSAgentServiceCommand = '{0} restart'.format(OMSAgentServiceScript)
DisableOMSAgentServiceCommand = '{0} disable'.format(OMSAgentServiceScript)

# Error codes
DPKGLockedErrorCode = 12
InstallErrorCurlNotInstalled = 64
EnableCalledBeforeSuccessfulInstall = 20
EnableErrorOMSReturned403 = 5
EnableErrorOMSReturnedNon200 = 6
EnableErrorResolvingHost = 7
UnsupportedOpenSSL = 60

# Configuration
HUtilObject = None
SettingsSequenceNumber = None
HandlerEnvironment = None
SettingsDict = None

# Change permission of log path - if we fail, that is not an exit case
try:
    ext_log_path = '/var/log/azure/'
    if os.path.exists(ext_log_path):
        os.chmod(ext_log_path, 700)
except:
    pass


def main():
    """
    Main method
    Parse out operation from argument, invoke the operation, and finish.
    """
    init_waagent_logger()
    waagent_log_info('OmsAgentForLinux started to handle.')

    # Determine the operation being executed
    operation = None
    try:
        option = sys.argv[1]
        if re.match('^([-/]*)(disable)', option):
            operation = 'Disable'
        elif re.match('^([-/]*)(uninstall)', option):
            operation = 'Uninstall'
        elif re.match('^([-/]*)(install)', option):
            operation = 'Install'
        elif re.match('^([-/]*)(enable)', option):
            operation = 'Enable'
        elif re.match('^([-/]*)(update)', option):
            operation = 'Update'
    except Exception as e:
        waagent_log_error(e.message)

    if operation is None:
        log_and_exit(None, 'Unknown', 1, 'No valid operation provided')

    # Set up for exit code and any error messages
    exit_code = 0
    message = '{0} succeeded'.format(operation)

    # Invoke operation
    try:
        global HUtilObject
        HUtilObject = parse_context(operation)
        exit_code = operations[operation]()

        # Exit code 1 indicates a general problem that doesn't have a more
        # specific error code; it often indicates a missing dependency
        if exit_code is 1 and operation == 'Install':
            message = 'Install failed with exit code 1. Please check that ' \
                      'dependencies are installed. For details, check logs ' \
                      'in /var/log/azure/Microsoft.EnterpriseCloud.' \
                      'Monitoring.OmsAgentForLinux'
        elif exit_code is DPKGLockedErrorCode and operation == 'Install':
            message = 'Install failed with exit code {0} because the ' \
                      'package manager on the VM is currently locked: ' \
                      'please wait and try again'.format(DPKGLockedErrorCode)
        elif exit_code is not 0:
            message = '{0} failed with exit code {1}'.format(operation,
                                                             exit_code)

    except OMSAgentParameterMissingError as e:
        exit_code = 11
        message = '{0} failed due to a missing parameter: ' \
                  '{1}'.format(operation, e.message)
    except OMSAgentInvalidParameterError as e:
        exit_code = 11
        message = '{0} failed due to an invalid parameter: ' \
                  '{1}'.format(operation, e.message)
    except OMSAgentUnwantedMultipleConnectionsException as e:
        exit_code = 10
        message = '{0} failed due to multiple connections: ' \
                  '{1}'.format(operation, e.message)
    except OMSAgentCannotConnectToOMSException as e:
        exit_code = 55 # error code to indicate no internet access
        message = 'The agent could not connect to the Microsoft Operations ' \
                  'Management Suite service. Please check that the system ' \
                  'either has Internet access, or that a valid HTTP proxy ' \
                  'has been configured for the agent. Please also check the ' \
                  'correctness of the workspace ID.'
    except Exception as e:
        exit_code = 1
        message = '{0} failed with error: {1}\n' \
                  'Stacktrace: {2}'.format(operation, e,
                                           traceback.format_exc())

    # Finish up and log messages
    log_and_exit(operation, exit_code, message)


def dummy_command():
    """
    Do nothing and return 0
    """
    return 0


def install():
    """
    Ensure that this VM distro and version are supported.
    Install the OMSAgent shell bundle, using retries.
    Note: install operation times out from WAAgent at 15 minutes, so do not
    wait longer.
    """
    exit_if_vm_not_supported('Install')

    public_settings, protected_settings = get_settings()
    if public_settings is None:
        raise OMSAgentParameterMissingError('Public configuration must be ' \
                                            'provided')
    workspaceId = public_settings.get('workspaceId')
    if workspaceId is None:
        raise OMSAgentParameterMissingError('Workspace ID must be provided')
    check_workspace_id(workspaceId)

    # In the case where a SCOM connection is already present, we should not
    # create conflicts by installing the OMSAgent packages
    stopOnMultipleConnections = public_settings.get('stopOnMultipleConnections')
    if (stopOnMultipleConnections is not None
            and stopOnMultipleConnections is True):
        detect_multiple_connections(workspaceId)

    package_directory = os.path.join(os.getcwd(), PackagesDirectory)
    bundle_path = os.path.join(package_directory, BundleFileName)

    os.chmod(bundle_path, 100)
    cmd = InstallCommandTemplate.format(bundle_path)
    hutil_log_info('Running command "{0}"'.format(cmd))

    # Retry, since install can fail due to concurrent package operations
    exit_code = run_command_with_retries(cmd, retries = 15,
                                         retry_check = retry_if_dpkg_locked_or_curl_is_not_found,
                                         final_check = final_check_if_dpkg_locked)
    return exit_code


def uninstall():
    """
    Uninstall the OMSAgent shell bundle.
    This is a somewhat soft uninstall. It is not a purge.
    Note: uninstall operation times out from WAAgent at 5 minutes
    """
    package_directory = os.path.join(os.getcwd(), PackagesDirectory)
    bundle_path = os.path.join(package_directory, BundleFileName)

    os.chmod(bundle_path, 100)
    cmd = UninstallCommandTemplate.format(bundle_path)
    hutil_log_info('Running command "{0}"'.format(cmd))

    # Retry, since uninstall can fail due to concurrent package operations
    exit_code = run_command_with_retries(cmd, retries = 5,
                                         retry_check = retry_if_dpkg_locked_or_curl_is_not_found,
                                         final_check = final_check_if_dpkg_locked)
    return exit_code


def enable():
    """
    Onboard the OMSAgent to the specified OMS workspace.
    This includes enabling the OMS process on the machine.
    This call will return non-zero or throw an exception if
    the settings provided are incomplete or incorrect.
    Note: enable operation times out from WAAgent at 5 minutes
    """
    exit_if_vm_not_supported('Enable')

    public_settings, protected_settings = get_settings()
    if public_settings is None:
        raise OMSAgentParameterMissingError('Public configuration must be ' \
                                            'provided')
    if protected_settings is None:
        raise OMSAgentParameterMissingError('Private configuration must be ' \
                                            'provided')

    workspaceId = public_settings.get('workspaceId')
    workspaceKey = protected_settings.get('workspaceKey')
    proxy = protected_settings.get('proxy')
    vmResourceId = protected_settings.get('vmResourceId')
    if workspaceId is None:
        raise OMSAgentParameterMissingError('Workspace ID must be provided')
    if workspaceKey is None:
        raise OMSAgentParameterMissingError('Workspace key must be provided')

    check_workspace_id_and_key(workspaceId, workspaceKey)

    # Check if omsadmin script is available
    if not os.path.exists(OMSAdminPath):
        log_and_exit('Enable', EnableCalledBeforeSuccessfulInstall,
                     'OMSAgent onboarding script {0} not exist. Enable ' \
                     'cannot be called before install.'.format(OMSAdminPath))

    proxyParam = ''
    if proxy is not None:
        proxyParam = '-p {0}'.format(proxy)

    vmResourceIdParam = ''
    if vmResourceId is not None:
        vmResourceIdParam = '-a {0}'.format(vmResourceId)

    optionalParams = '{0} {1}'.format(proxyParam, vmResourceIdParam)
    onboard_cmd = OnboardCommandWithOptionalParamsTemplate.format(OMSAdminPath,
                                                                  workspaceId,
                                                                  workspaceKey,
                                                                  optionalParams)

    hutil_log_info('Handler initiating onboarding.')
    exit_code = run_command_with_retries(onboard_cmd, retries = 5,
                                         retry_check = retry_onboarding,
                                         final_check = raise_if_no_internet,
                                         check_error = True, log_cmd = False)

    # Sleep to prevent bombarding the processes, then restart all processes to
    # resolve any issues with auto-started processes from --upgrade
    if exit_code is 0:
        time.sleep(5) # 5 seconds
        run_command_and_log(RestartOMSAgentServiceCommand)

    return exit_code


def disable():
    """
    Disable all OMS workspace processes on the machine.
    Note: disable operation times out from WAAgent at 15 minutes
    """
    # Check if the service control script is available
    if not os.path.exists(OMSAgentServiceScript):
        log_and_exit('Disable', 1, 'OMSAgent service control script {0} ' \
                                   'does not exist. Disable cannot be ' \
                                   'called before install.'.format(OMSAgentServiceScript))
        return 1

    exit_code, output = run_command_and_log(DisableOMSAgentServiceCommand)
    return exit_code


# Dictionary of operations strings to methods
operations = {'Disable' : disable,
              'Uninstall' : uninstall,
              'Install' : install,
              'Enable' : enable,
              # Upgrade is noop since omsagent.py->install() will be called
              # everytime upgrade is done due to upgradeMode =
              # "UpgradeWithInstall" set in HandlerManifest
              'Update' : dummy_command
}


def parse_context(operation):
    """
    Initialize a HandlerUtil object for this operation.
    If the required modules have not been imported, this will return None.
    """
    hutil = None
    if 'Utils.WAAgentUtil' in sys.modules and 'Utils.HandlerUtil' in sys.modules:
        try:
            hutil = HUtil.HandlerUtility(waagent.Log, waagent.Error)
            hutil.do_parse_context(operation)
        # parse_context may throw KeyError if necessary JSON key is not
        # present in settings
        except KeyError as e:
            waagent_log_error('Unable to parse context with error: ' \
                              '{0}'.format(e.message))
            raise OMSAgentParameterMissingError
    return hutil


def is_vm_supported_for_extension():
    """
    Checks if the VM this extension is running on is supported by OMSAgent
    Returns for platform.linux_distribution() vary widely in format, such as
    '7.3.1611' returned for a machine with CentOS 7, so the first provided
    digits must match
    The supported distros of the OMSAgent-for-Linux, as well as Ubuntu 16.10,
    are allowed to utilize this VM extension. All other distros will get
    error code 51
    """
    supported_dists = {'redhat' : ('5', '6', '7'), # CentOS
                       'centos' : ('5', '6', '7'), # CentOS
                       'red hat' : ('5', '6', '7'), # Oracle, RHEL
                       'oracle' : ('5', '6', '7'), # Oracle
                       'debian' : ('6', '7', '8'), # Debian
                       'ubuntu' : ('12.04', '14.04', '15.04', '15.10',
                                   '16.04', '16.10'), # Ubuntu
                       'suse' : ('11', '12') #SLES
    }

    try:
        vm_dist, vm_ver, vm_id = platform.linux_distribution()
    except AttributeError:
        vm_dist, vm_ver, vm_id = platform.dist()

    vm_supported = False

    # Find this VM distribution in the supported list
    for supported_dist in supported_dists.keys():
        if not vm_dist.lower().startswith(supported_dist):
            continue

        # Check if this VM distribution version is supported
        vm_ver_split = vm_ver.split('.')
        for supported_ver in supported_dists[supported_dist]:
            supported_ver_split = supported_ver.split('.')

            # If vm_ver is at least as precise (at least as many digits) as
            # supported_ver and matches all the supported_ver digits, then
            # this VM is guaranteed to be supported
            vm_ver_match = True
            for idx, supported_ver_num in enumerate(supported_ver_split):
                try:
                    supported_ver_num = int(supported_ver_num)
                    vm_ver_num = int(vm_ver_split[idx])
                except IndexError:
                    vm_ver_match = False
                    break
                if vm_ver_num is not supported_ver_num:
                    vm_ver_match = False
                    break
            if vm_ver_match:
                vm_supported = True
                break

        if vm_supported:
            break

    return vm_supported, vm_dist, vm_ver


def exit_if_vm_not_supported(operation):
    """
    Check if this VM distro and version are supported by the OMSAgent.
    If this VM is not supported, log the proper error code and exit.
    """
    vm_supported, vm_dist, vm_ver = is_vm_supported_for_extension()
    if not vm_supported:
        log_and_exit(operation, 51, 'Unsupported operation system: ' \
                                    '{0} {1}'.format(vm_dist, vm_ver))
    return 0


def exit_if_openssl_unavailable(operation):
    """
    Check if the openssl commandline interface is available to use
    If not, throw error to return UnsupportedOpenSSL error code
    """
    exit_code, output = run_get_output('which openssl', True, False)
    if exit_code is not 0:
        log_and_exit(operation, UnsupportedOpenSSL, 'OpenSSL is not available')
    return 0


def check_workspace_id_and_key(workspace_id, workspace_key):
    """
    Validate formats of workspace_id and workspace_key
    """
    check_workspace_id(workspace_id)
    # Validate that workspace_key is of the correct format (base64-encoded)
    try:
        encoded_key = base64.b64encode(base64.b64decode(workspace_key))
        if encoded_key != workspace_key:
            raise OMSAgentInvalidParameterError('Workspace key is invalid')
    except TypeError:
        raise OMSAgentInvalidParameterError('Workspace key is invalid')


def check_workspace_id(workspace_id):
    """
    Validate that workspace_id matches the GUID regex
    """
    search = re.compile(GUIDOnlyRegex, re.M)
    if not search.match(workspace_id):
        raise OMSAgentInvalidParameterError('Workspace ID is invalid')


def detect_multiple_connections(workspace_id):
    """
    If the VM already has a workspace/SCOM configured, then we should
    disallow a new connection when stopOnMultipleConnections is used

    Throw an exception in these cases:
    - The workspace with the given workspace_id has not been onboarded
      to the VM, but at least one other workspace has been
    - The workspace with the given workspace_id has not been onboarded
      to the VM, and the VM is connected to SCOM

    If the extension operation is connecting to an already-configured
    workspace, it is not a stopping case
    """
    other_connection_exists = False
    if os.path.exists(OMSAdminPath):
        exit_code, output = run_get_output(WorkspaceCheckCommand,
                                           chk_err = False)

        if output.strip().lower() != 'no workspace':
            for line in output.split('\n'):
                if workspace_id in line:
                    hutil_log_info('The workspace to be enabled has already ' \
                                   'been configured on the VM before; ' \
                                   'continuing despite ' \
                                   'stopOnMultipleConnections flag')
                    return
                else:
                    # Note: if scom workspace dir is created, a line containing
                    # "Workspace(SCOM Workspace): scom" will be here
                    # If any other line is here, it may start sending data later
                    other_connection_exists = True
    else:
        for dir_name, sub_dirs, files in os.walk(EtcOMSAgentPath):
            for sub_dir in sub_dirs:
                sub_dir_name = os.path.basename(sub_dir)
                workspace_search = re.compile(GUIDOnlyRegex, re.M)
                if sub_dir_name == workspace_id:
                    hutil_log_info('The workspace to be enabled has already ' \
                                   'been configured on the VM before; ' \
                                   'continuing despite ' \
                                   'stopOnMultipleConnections flag')
                    return
                elif workspace_search.match(sub_dir_name) or sub_dir_name == 'scom':
                    other_connection_exists = True

    if other_connection_exists:
        err_msg = ('This machine is already connected to some other Log ' \
                   'Analytics workspace, please set ' \
                   'stopOnMultipleConnections to false in public ' \
                   'settings or remove this property, so this machine ' \
                   'can connect to new workspaces, also it means this ' \
                   'machine will get billed multiple times for each ' \
                   'workspace it report to. ' \
                   '(LINUXOMSAGENTEXTENSION_ERROR_MULTIPLECONNECTIONS)')
        # This exception will get caught by the main method
        raise OMSAgentUnwantedMultipleConnectionsException(err_msg)
    else:
        detect_scom_connection()


def detect_scom_connection():
    """
    If these two conditions are met, then we can assume the
    VM is monitored
    by SCOM:
    1. SCOMPort is open and omiserver is listening on it
    2. scx certificate is signed by SCOM server

    To determine it check for existence of below two
    conditions:
    1. SCOMPort is open and omiserver is listening on it:
       /etc/omi/conf/omiserver.conf can be parsed to
       determine it.
    2. scx certificate is signed by SCOM server: scom cert
       is present @ /etc/opt/omi/ssl/omi-host-<hostname>.pem
       (/etc/opt/microsoft/scx/ssl/scx.pem is a softlink to
       this). If the machine is monitored by SCOM then issuer
       field of the certificate will have a value like
       CN=SCX-Certificate/title=<GUID>, DC=<SCOM server hostname>
       (e.g CN=SCX-Certificate/title=SCX94a1f46d-2ced-4739-9b6a-1f06156ca4ac,
       DC=NEB-OM-1502733)

    Otherwise, if a scom configuration directory has been
    created, we assume SCOM is in use
    """
    scom_port_open = None # return when determine this is false
    cert_signed_by_scom = False

    if os.path.exists(OMSAdminPath):
        scom_port_open = detect_scom_using_omsadmin()
        if scom_port_open is False:
            return

    # If omsadmin.sh option is not available, use omiconfigeditor
    if (scom_port_open is None and os.path.exists(OMIConfigEditorPath)
            and os.path.exists(OMIServerConfPath)):
        scom_port_open = detect_scom_using_omiconfigeditor()
        if scom_port_open is False:
            return

    # If omiconfigeditor option is not available, directly parse omiserver.conf
    if scom_port_open is None and os.path.exists(OMIServerConfPath):
        scom_port_open = detect_scom_using_omiserver_conf()
        if scom_port_open is False:
            return

    if scom_port_open is None:
        hutil_log_info('SCOM port could not be determined to be open')
        return

    # Parse the certificate to determine if SCOM issued it
    if os.path.exists(SCOMCertPath):
        exit_if_openssl_unavailable('Install')
        cert_cmd = 'openssl x509 -in {0} -noout -text'.format(SCOMCertPath)
        cert_exit_code, cert_output = run_get_output(cert_cmd, chk_err = False,
                                                     log_cmd = False)
        if cert_exit_code is 0:
            issuer_re = re.compile(SCOMCertIssuerRegex, re.M)
            if issuer_re.search(cert_output):
                hutil_log_info('SCOM cert exists and is signed by SCOM server')
                cert_signed_by_scom = True
            else:
                hutil_log_info('SCOM cert exists but is not signed by SCOM ' \
                               'server')
        else:
            hutil_log_error('Error reading SCOM cert; cert could not be ' \
                            'determined to be signed by SCOM server')
    else:
        hutil_log_info('SCOM cert does not exist')

    if scom_port_open and cert_signed_by_scom:
        err_msg = ('This machine may already be connected to a System ' \
                   'Center Operations Manager server. Please set ' \
                   'stopOnMultipleConnections to false in public settings ' \
                   'or remove this property to allow connection to the Log ' \
                   'Analytics workspace. ' \
                   '(LINUXOMSAGENTEXTENSION_ERROR_MULTIPLECONNECTIONS)')
        raise OMSAgentUnwantedMultipleConnectionsException(err_msg)


def detect_scom_using_omsadmin():
    """
    This method assumes that OMSAdminPath exists; if packages have not
    been installed yet, this may not exist
    Returns True if omsadmin.sh indicates that SCOM port is open
    """
    omsadmin_cmd = '{0} -o'.format(OMSAdminPath)
    exit_code, output = run_get_output(omsadmin_cmd, False, False)
    # Guard against older omsadmin.sh versions
    if ('illegal option' not in output.lower()
            and 'unknown option' not in output.lower()):
        if exit_code is 0:
            hutil_log_info('According to {0}, SCOM port is ' \
                           'open'.format(omsadmin_cmd))
            return True
        elif exit_code is 1:
            hutil_log_info('According to {0}, SCOM port is not ' \
                           'open'.format(omsadmin_cmd))
    return False


def detect_scom_using_omiconfigeditor():
    """
    This method assumes that the relevant files exist
    Returns True if omiconfigeditor indicates that SCOM port is open
    """
    omi_cmd = '{0} httpsport -q {1} < {2}'.format(OMIConfigEditorPath,
                                                  SCOMPort, OMIServerConfPath)
    exit_code, output = run_get_output(omi_cmd, False, False)
    # Guard against older omiconfigeditor versions
    if ('illegal option' not in output.lower()
            and 'unknown option' not in output.lower()):
        if exit_code is 0:
            hutil_log_info('According to {0}, SCOM port is ' \
                           'open'.format(omi_cmd))
            return True
        elif exit_code is 1:
            hutil_log_info('According to {0}, SCOM port is not ' \
                           'open'.format(omi_cmd))
    return False


def detect_scom_using_omiserver_conf():
    """
    This method assumes that the relevant files exist
    Returns True if omiserver.conf indicates that SCOM port is open
    """
    with open(OMIServerConfPath, 'r') as omiserver_file:
        omiserver_txt = omiserver_file.read()

    httpsport_search = r'^[\s]*httpsport[\s]*=(.*)$'
    httpsport_re = re.compile(httpsport_search, re.M)
    httpsport_matches = httpsport_re.search(omiserver_txt)
    if (httpsport_matches is not None and
            httpsport_matches.group(1) is not None):
        ports = httpsport_matches.group(1)
        ports = ports.replace(',', ' ')
        ports_list = ports.split(' ')
        if str(SCOMPort) in ports_list:
            hutil_log_info('SCOM port is listed in ' \
                           '{0}'.format(OMIServerConfPath))
            return True
        else:
            hutil_log_info('SCOM port is not listed in ' \
                           '{0}'.format(OMIServerConfPath))
    else:
        hutil_log_info('SCOM port is not listed in ' \
                           '{0}'.format(OMIServerConfPath))
    return False


def run_command_and_log(cmd, check_error = True, log_cmd = True):
    """
    Run the provided shell command and log its output, including stdout and
    stderr.
    The output should not contain any PII, but the command might. In this case,
    log_cmd should be set to False.
    """
    exit_code, output = run_get_output(cmd, check_error, log_cmd)
    if log_cmd:
        hutil_log_info('Output of command "{0}": \n{1}'.format(cmd, output))
    else:
        hutil_log_info('Output: \n{0}'.format(output))
    return exit_code, output


def run_command_with_retries(cmd, retries, retry_check, final_check = None,
                             check_error = True, log_cmd = True,
                             initial_sleep_time = 30,
                             sleep_increase_factor = 1):
    """
    Caller provides a method, retry_check, to use to determine if a retry
    should be performed. This must be a function with two parameters:
    exit_code and output
    The final_check can be provided as a method to perform a final check after
    retries have been exhausted
    Logic used: will retry up to retries times with initial_sleep_time in
    between tries
    """
    try_count = 0
    sleep_time = initial_sleep_time # seconds

    while try_count <= retries:
        exit_code, output = run_command_and_log(cmd, check_error, log_cmd)
        should_retry, retry_message = retry_check(exit_code, output)
        if not should_retry:
            break
        try_count += 1
        hutil_log_info(retry_message)
        time.sleep(sleep_time)
        sleep_time *= sleep_increase_factor

    if final_check is not None:
        exit_code = final_check(exit_code, output)

    return exit_code


def is_dpkg_locked(exit_code, output):
    """
    If dpkg is locked, the output will contain a message similar to 'dpkg 
    status database is locked by another process'
    """
    if exit_code is not 0:
        dpkg_locked_search = r'^.*dpkg.+lock.*$'
        dpkg_locked_re = re.compile(dpkg_locked_search, re.M)
        if dpkg_locked_re.search(output):
            return True
    return False


def is_curl_found(exit_code, output):
    """
    Returns false if exit_code indicates that curl was not installed; this can
    occur when package lists need to be updated, or when some archives are
    out-of-date
    """
    if exit_code is InstallErrorCurlNotInstalled:
        return False
    return True


def retry_if_dpkg_locked_or_curl_is_not_found(exit_code, output):
    """
    Some commands fail because the package manager is locked (apt-get/dpkg
    only); this will allow retries on failing commands.
    Sometimes curl is not installed and is also not found in the package list;
    if this is the case on a machine with apt-get, update the package list
    """
    dpkg_locked = is_dpkg_locked(exit_code, output)
    curl_found = is_curl_found(exit_code, output)
    apt_get_exit_code, apt_get_output = run_get_output('which apt-get',
                                                       chk_err = False,
                                                       log_cmd = False)
    if dpkg_locked:
        return True, 'Retrying command because package manager is locked.'
    elif not curl_found and apt_get_exit_code is 0:
        hutil_log_info('Updating package lists to make curl available')
        run_command_and_log('apt-get update')
        return True, 'Retrying command because package lists needed to be ' \
                     'updated'
    else:
        return False, ''


def final_check_if_dpkg_locked(exit_code, output):
    """
    If dpkg is still locked after the retries, we want to return a specific
    error code
    """
    dpkg_locked = is_dpkg_locked(exit_code, output)
    if dpkg_locked:
        exit_code = DPKGLockedErrorCode
    return exit_code


def retry_onboarding(exit_code, output):
    """
    Retry under any of these conditions:
    - If the onboarding request returns 403: this may indicate that the agent
      GUID and certificate should be re-generated
    - If the onboarding request returns a different non-200 code: the OMS
      service may be temporarily unavailable
    """
    if exit_code is EnableErrorOMSReturned403:
        return True, 'Retrying the onboarding command to attempt generating ' \
                     'a new agent ID and certificate.'
    elif exit_code is EnableErrorOMSReturnedNon200:
        return True, 'Retrying; the OMS service may be temporarily ' \
                     'unavailable.'
    return False, ''


def raise_if_no_internet(exit_code, output):
    """
    Raise the OMSAgentCannotConnectToOMSException exception if the onboarding
    script returns the error code to indicate that the OMS service can't be
    resolved
    """
    if exit_code is EnableErrorResolvingHost:
        raise OMSAgentCannotConnectToOMSException
    return exit_code


def get_settings():
    """
    Retrieve the configuration for this extension operation
    """
    global SettingsDict
    public_settings = None
    protected_settings = None

    if HUtilObject is not None:
        public_settings = HUtilObject.get_public_settings()
        protected_settings = HUtilObject.get_protected_settings()
    elif SettingsDict is not None:
        public_settings = SettingsDict['public_settings']
        protected_settings = SettingsDict['protected_settings']
    else:
        SettingsDict = {}
        handler_env = get_handler_env()
        try:
            config_dir = str(handler_env['handlerEnvironment']['configFolder'])
        except:
            config_dir = os.path.join(os.getcwd(), 'config')

        seq_no = get_latest_seq_no()
        settings_path = os.path.join(config_dir, '{0}.settings'.format(seq_no))
        try:
            with open(settings_path, 'r') as settings_file:
                settings_txt = settings_file.read()
            settings = json.loads(settings_txt)
            h_settings = settings['runtimeSettings'][0]['handlerSettings']
            public_settings = h_settings['publicSettings']
            SettingsDict['public_settings'] = public_settings
        except:
            hutil_log_error('Unable to load handler settings from ' \
                            '{0}'.format(settings_path))

        if (h_settings.has_key('protectedSettings')
                and h_settings.has_key('protectedSettingsCertThumbprint')
                and h_settings['protectedSettings'] is not None
                and h_settings['protectedSettingsCertThumbprint'] is not None):
            encoded_settings = h_settings['protectedSettings']
            settings_thumbprint = h_settings['protectedSettingsCertThumbprint']
            encoded_cert_path = os.path.join('/var/lib/waagent',
                                             '{0}.crt'.format(
                                                       settings_thumbprint))
            encoded_key_path = os.path.join('/var/lib/waagent',
                                            '{0}.prv'.format(
                                                      settings_thumbprint))
            decoded_settings = base64.standard_b64decode(encoded_settings)
            decrypt_cmd = 'openssl smime -inform DER -decrypt -recip {0} ' \
                                   '-inkey {1}'.format(encoded_cert_path,
                                                       encoded_key_path)

            try:
                session = subprocess.Popen([decrypt_cmd], shell = True,
                                           stdin = subprocess.PIPE,
                                           stderr = subprocess.STDOUT,
                                           stdout = subprocess.PIPE)
                output = session.communicate(decoded_settings)
            except OSError, e:
                pass
            protected_settings_str = output[0]

            if protected_settings_str is None:
                log_and_exit('Enable', 1, 'Failed decrypting ' \
                                                 'protectedSettings')
            protected_settings = ''
            try:
                protected_settings = json.loads(protected_settings_str)
            except:
                hutil_log_error('JSON exception decoding protected settings')
            SettingsDict['protected_settings'] = protected_settings

    return public_settings, protected_settings


def update_status_file(operation, exit_code, exit_status, message):
    """
    Mimic HandlerUtil method do_status_report in case hutil method is not
    available
    Write status to status file
    """
    handler_env = get_handler_env()
    try:
        extension_version = str(handler_env['version'])
        config_dir = str(handler_env['handlerEnvironment']['configFolder'])
        status_dir = str(handler_env['handlerEnvironment']['statusFolder'])
    except:
        extension_version = "1.0"
        config_dir = os.path.join(os.getcwd(), 'config')
        status_dir = os.path.join(os.getcwd(), 'status')

    status_txt = [{
        "version" : extension_version,
        "timestampUTC" : time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status" : {
            "name" : "Microsoft.EnterpriseCloud.Monitoring.OmsAgentForLinux",
            "operation" : operation,
            "status" : exit_status,
            "code" : exit_code,
            "formattedMessage" : {
                "lang" : "en-US",
                "message" : message
            }
        }
    }]

    status_json = json.dumps(status_txt)

    # Find the most recently changed config file and then use the
    # corresponding status file
    latest_seq_no = get_latest_seq_no()

    status_path = os.path.join(status_dir, '{0}.status'.format(latest_seq_no))
    status_tmp = '{0}.tmp'.format(status_path)
    with open(status_tmp, 'w+') as tmp_file:
        tmp_file.write(status_json)
    os.rename(status_tmp, status_path)


def get_handler_env():
    """
    Set and retrieve the contents of HandlerEnvironment.json as JSON
    """
    global HandlerEnvironment
    if HandlerEnvironment is None:
        handler_env_path = os.path.join(os.getcwd(), 'HandlerEnvironment.json')
        try:
            with open(handler_env_path, 'r') as handler_env_file:
                handler_env_txt = handler_env_file.read()
            handler_env=json.loads(handler_env_txt)
            if type(handler_env) == list:
                handler_env = handler_env[0]
            HandlerEnvironment = handler_env
        except Exception as e:
            waagent_log_error(e.message)
    return HandlerEnvironment


def get_latest_seq_no():
    """
    Determine the latest operation settings number to use
    """
    global SettingsSequenceNumber
    if SettingsSequenceNumber is None:
        handler_env = get_handler_env()
        try:
            config_dir = str(handler_env['handlerEnvironment']['configFolder'])
        except:
            config_dir = os.path.join(os.getcwd(), 'config')

        latest_seq_no = -1
        cur_seq_no = -1
        latest_time = None
        try:
            for dir_name, sub_dirs, files in os.walk(config_dir):
                for file in files:
                    file_basename = os.path.basename(file)
                    match = re.match(r'[0-9]{1,10}\.settings', file_basename)
                    if match is None:
                        continue
                    cur_seq_no = int(file_basename.split('.')[0])
                    file_path = os.path.join(config_dir, file)
                    cur_time = os.path.getmtime(file_path)
                    if latest_time is None or cur_time > latest_time:
                        latest_time = cur_time
                        latest_seq_no = cur_seq_no
        except:
            pass
        if latest_seq_no < 0:
            latest_seq_no = 0    
        SettingsSequenceNumber = latest_seq_no

    return SettingsSequenceNumber


def run_get_output(cmd, chk_err = False, log_cmd = True):
    """
    Mimic waagent mothod RunGetOutput in case waagent is not available
    Run shell command and return exit code and output
    """
    if 'Utils.WAAgentUtil' in sys.modules:
        # WALinuxAgent-2.0.14 allows only 2 parameters for RunGetOutput
        # If checking the number of parameters fails, pass 2
        try:
            sig = inspect.signature(waagent.RunGetOutput)
            params = sig.parameters
            waagent_params = len(params)
        except:
            try:
                spec = inspect.getargspec(waagent.RunGetOutput)
                params = spec.args
                waagent_params = len(params)
            except:
                waagent_params = 2
        if waagent_params >= 3:
            exit_code, output = waagent.RunGetOutput(cmd, chk_err, log_cmd)
        else:
            exit_code, output = waagent.RunGetOutput(cmd, chk_err)
    else:
        try:
            output = subprocess.check_output(cmd, stderr = subprocess.STDOUT,
                                             shell = True)
            exit_code = 0
        except subprocess.CalledProcessError as e:
            exit_code = e.returncode
            output = e.output

    return exit_code, output.encode('utf-8').strip()


def init_waagent_logger():
    """
    Initialize waagent logger
    If waagent has not been imported, catch the exception
    """
    try:
        waagent.LoggerInit('/var/log/waagent.log','/dev/stdout', True)
    except Exception as e:
        print('Unable to initialize waagent log because of exception ' \
              '{0}'.format(e))


def waagent_log_info(message):
    """
    Log informational message, being cautious of possibility that waagent may
    not be imported
    """
    if 'Utils.WAAgentUtil' in sys.modules:
        waagent.Log(message)
    else:
        print('Info: {0}'.format(message))


def waagent_log_error(message):
    """
    Log error message, being cautious of possibility that waagent may not be
    imported
    """
    if 'Utils.WAAgentUtil' in sys.modules:
        waagent.Error(message)
    else:
        print('Error: {0}'.format(message))


def hutil_log_info(message):
    """
    Log informational message, being cautious of possibility that hutil may
    not be imported and configured
    """
    if HUtilObject is not None:
        HUtilObject.log(message)
    else:
        print('Info: {0}'.format(message))


def hutil_log_error(message):
    """
    Log error message, being cautious of possibility that hutil may not be
    imported and configured
    """
    if HUtilObject is not None:
        HUtilObject.error(message)
    else:
        print('Error: {0}'.format(message))


def log_and_exit(operation, exit_code = 1, message = ''):
    """
    Log the exit message and perform the exit
    """
    if exit_code is 0:
        waagent_log_info(message)
        hutil_log_info(message)
        exit_status = 'success'
    else:
        waagent_log_error(message)
        hutil_log_error(message)
        exit_status = 'failed'

    if HUtilObject is not None:
        HUtilObject.do_exit(exit_code, operation, exit_status, str(exit_code), message)
    else:
        update_status_file(operation, str(exit_code), exit_status, message)
        sys.exit(exit_code)


class OMSAgentParameterMissingError(ValueError):
    """
    There is a missing parameter for the OmsAgentForLinux Extension
    """
    pass


class OMSAgentInvalidParameterError(ValueError):
    """
    There is an invalid parameter for the OmsAgentForLinux Extension
    ex. Workspace ID does not match GUID regex
    """
    pass


class OMSAgentUnwantedMultipleConnectionsException(Exception):
    """
    This machine is already connected to a different Log Analytics workspace
    and stopOnMultipleConnections is set to true
    """
    pass


class OMSAgentCannotConnectToOMSException(Exception):
    """
    The OMSAgent cannot connect to the OMS service
    """
    pass


if __name__ == '__main__' :
    main()
