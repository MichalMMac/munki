#!/usr/bin/python
# encoding: utf-8
#
# Copyright 2009-2014 Greg Neagle.
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
managedsoftwareupdate
"""

import optparse
import os
import re
import signal
import subprocess
import sys
import time
import traceback

# Do not place any imports with ObjC bindings above this!
try:
    from Foundation import NSDate
    from Foundation import NSDistributedNotificationCenter
    from Foundation import NSNotificationDeliverImmediately
    from Foundation import NSNotificationPostToAllSessions
except ImportError:
    # Python is missing ObjC bindings. Run external report script.
    from munkilib import utils
    print >> sys.stderr, 'Python is missing ObjC bindings.'
    scriptdir = os.path.realpath(os.path.dirname(sys.argv[0]))
    script = os.path.join(scriptdir, 'report_broken_client')
    try:
        result, stdout, stderr = utils.runExternalScript(script)
        print >> sys.stderr, result, stdout, stderr
    except utils.ScriptNotFoundError:
        pass  # script is not required, so pass
    except utils.RunExternalScriptError, err:
        print >> sys.stderr, str(err)
    sys.exit(200)
else:
    from munkilib import munkicommon
    from munkilib import updatecheck
    from munkilib import installer
    from munkilib import munkistatus
    from munkilib import appleupdates
    from munkilib import FoundationPlist
    from munkilib import utils


def signal_handler(signum, dummy_frame):
    """Handle any signals we've been told to.
    Right now just handle SIGTERM so clean up can happen, like
    garbage collection, which will trigger object destructors and
    kill any launchd processes we've started."""
    if signum == signal.SIGTERM:
        sys.exit()


def getIdleSeconds():
    """Returns the number of seconds since the last mouse
    or keyboard event."""
    cmd = ['/usr/sbin/ioreg', '-c', 'IOHIDSystem']
    proc = subprocess.Popen(cmd, shell=False, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (output, dummy_err) = proc.communicate()
    ioreglines = str(output).splitlines()
    idle_time = 0
    regex = re.compile(r'"?HIDIdleTime"?\s+=\s+(\d+)')
    for line in ioreglines:
        idle_re = regex.search(line)
        if idle_re:
            idle_time = idle_re.group(1)
            break
    return int(int(idle_time)/1000000000)


def networkUp():
    """Determine if the network is up by looking for any non-loopback
       internet network interfaces.

    Returns:
      Boolean. True if loopback is found (network is up), False otherwise.
    """
    cmd = ['/sbin/ifconfig', '-a', 'inet']
    proc = subprocess.Popen(cmd, shell=False, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (output, dummy_err) = proc.communicate()
    lines = str(output).splitlines()
    for line in lines:
        if 'inet' in line:
            parts = line.split()
            addr = parts[1]
            if not addr in ['127.0.0.1', '0.0.0.0']:
                return True
    return False


def clearLastNotifiedDate():
    """Clear the last date the user was notified of updates."""
    munkicommon.set_pref('LastNotifiedDate', None)


def createDirsIfNeeded(dirlist):
    """Create any missing directories needed by the munki tools.

    Args:
      dirlist: a sequence of directories.
    Returns:
      Boolean. True if all directories existed or were created,
      False otherwise.
    """
    for directory in dirlist:
        if not os.path.exists(directory):
            try:
                os.mkdir(directory)
            except (OSError, IOError):
                print >> sys.stderr, 'ERROR: Could not create %s' % directory
                return False

    return True


def initMunkiDirs():
    """Figure out where data directories should be and create them if needed.

    Returns:
      Boolean. True if all data dirs existed or were created, False otherwise.
    """
    ManagedInstallDir = munkicommon.pref('ManagedInstallDir')
    manifestsdir = os.path.join(ManagedInstallDir, 'manifests')
    catalogsdir = os.path.join(ManagedInstallDir, 'catalogs')
    iconsdir = os.path.join(ManagedInstallDir, 'icons')
    cachedir = os.path.join(ManagedInstallDir, 'Cache')
    logdir = os.path.join(ManagedInstallDir, 'Logs')

    if not createDirsIfNeeded([ManagedInstallDir, manifestsdir, catalogsdir,
                               iconsdir, cachedir, logdir]):
        munkicommon.display_error('Could not create needed directories '
                                  'in %s' % ManagedInstallDir)
        return False
    else:
        return True


def runScript(script, display_name, runtype):
    """Run an external script. Do not run if the permissions on the external
    script file are weaker than the current executable."""
    result = 0
    if os.path.exists(script):
        munkicommon.display_status_minor(
            'Performing %s tasks...' % display_name)
    else:
        return result

    try:
        utils.verifyFileOnlyWritableByMunkiAndRoot(script)
    except utils.VerifyFilePermissionsError, err:
        # preflight/postflight is insecure, but if the currently executing
        # file is insecure too we are no worse off.
        try:
            utils.verifyFileOnlyWritableByMunkiAndRoot(__file__)
        except utils.VerifyFilePermissionsError, err:
            # OK, managedsoftwareupdate is insecure anyway - warn & execute.
            munkicommon.display_warning('Multiple munki executable scripts '
                'have insecure file permissions. Executing '
                '%s anyway. Error: %s' % (display_name, err))
        else:
            # Just the preflight/postflight is insecure. Do not execute.
            munkicommon.display_warning('Skipping execution of %s due to '
                'insecure file permissions. Error: %s' % (display_name, err))
            return result

    try:
        result, stdout, stderr = utils.runExternalScript(
            script, allow_insecure=True, script_args=[runtype])
        if result:
            munkicommon.display_info('%s return code: %d'
                                    % (display_name, result))
        if stdout:
            munkicommon.display_info('%s stdout: %s' % (display_name, stdout))
        if stderr:
            munkicommon.display_info('%s stderr: %s' % (display_name, stderr))
    except utils.ScriptNotFoundError:
        pass  # script is not required, so pass
    except utils.RunExternalScriptError, err:
        munkicommon.display_warning(str(err))
    return result


def doInstallTasks(do_apple_updates, only_unattended=False):
    """Perform our installation/removal tasks.

    Args:
      do_apple_updates: Boolean. If True, install Apple updates
      only_unattended:  Boolean. If True, only do unattended_(un)install items.

    Returns:
      Boolean. True if a restart is required, False otherwise.
    """
    if not only_unattended:
        # first, clear the last notified date
        # so we can get notified of new changes after this round
        # of installs
        clearLastNotifiedDate()

    munki_need_to_restart = False
    apple_need_to_restart = False

    if munkiUpdatesAvailable():
        # install munki updates
        try:
            munki_need_to_restart = installer.run(only_unattended=only_unattended)
        except KeyboardInterrupt:
            munkicommon.savereport()
            raise
        except:
            munkicommon.display_error('Unexpected error in munkilib.installer:')
            munkicommon.log(traceback.format_exc())
            munkicommon.savereport()
            raise

    if do_apple_updates:
        # install Apple updates
        try:
            apple_need_to_restart = appleupdates.installAppleUpdates(
                                            only_unattended=only_unattended)
        except KeyboardInterrupt:
            munkicommon.savereport()
            raise
        except:
            munkicommon.display_error(
                'Unexpected error in appleupdates.installAppleUpdates:')
            munkicommon.log(traceback.format_exc())
            munkicommon.savereport()
            raise

    munkicommon.savereport()
    return munki_need_to_restart or apple_need_to_restart


def startLogoutHelper():
    """Handle the need for a forced logout. Start our logouthelper"""
    cmd = ['/bin/launchctl', 'start', 'com.googlecode.munki.logouthelper']
    result = subprocess.call(cmd)
    if result:
        # some problem with the launchd job
        munkicommon.display_error(
            'Could not start com.googlecode.munki.logouthelper')


def doRestart():
    """Handle the need for a restart."""
    restartMessage = 'Software installed or removed requires a restart.'
    munkicommon.log(restartMessage)
    if munkicommon.munkistatusoutput:
        munkistatus.hideStopButton()
        munkistatus.message(restartMessage)
        munkistatus.detail('')
        munkistatus.percent(-1)
    else:
        munkicommon.display_info(restartMessage)

    # check current console user
    consoleuser = munkicommon.getconsoleuser()
    if not consoleuser or consoleuser == u'loginwindow':
        # no-one is logged in or we're at the loginwindow
        time.sleep(5)
        dummy_retcode = subprocess.call(['/sbin/shutdown', '-r', 'now'])
    else:
        if munkicommon.munkistatusoutput:
            # someone is logged in and we're using Managed Software Center.
            # We need to notifiy the active user that a restart is required.
            # We actually should almost never get here; generally Munki knows
            # a restart is needed before even starting the updates and forces
            # a logout before applying the updates
            munkicommon.display_info(
                'Notifying currently logged-in user to restart.')
            munkistatus.activate()
            munkistatus.restartAlert()
            # Managed Software Center will trigger a restart
            # when the alert is dismissed. If a user gets clever and subverts
            # this restart (perhaps by force-quitting the app),
            # that's their problem...
        else:
            print 'Please restart immediately.'


def munkiUpdatesAvailable():
    """Return count of available updates."""
    updatesavailable = 0
    installinfo = os.path.join(munkicommon.pref('ManagedInstallDir'),
                               'InstallInfo.plist')
    if os.path.exists(installinfo):
        try:
            plist = FoundationPlist.readPlist(installinfo)
            updatesavailable = (len(plist.get('removals', [])) +
                                len(plist.get('managed_installs', [])))
        except (AttributeError,
                FoundationPlist.NSPropertyListSerializationException):
            munkicommon.display_error('Install info at %s is invalid.' %
                                       installinfo)
    return updatesavailable


def munkiUpdatesContainAppleItems():
    """Return True if there are any Apple items in the list of updates"""
    installinfo = os.path.join(munkicommon.pref('ManagedInstallDir'),
                               'InstallInfo.plist')
    if os.path.exists(installinfo):
        try:
            plist = FoundationPlist.readPlist(installinfo)
        except FoundationPlist.NSPropertyListSerializationException:
            munkicommon.display_error('Install info at %s is invalid.' %
                                           installinfo)
        else:
            # check managed_installs
            for item in plist.get('managed_installs', []):
                if item.get('apple_item'):
                    return True
            # check removals
            for item in plist.get('removals', []):
                if item.get('apple_item'):
                    return True
    return False


def recordUpdateCheckResult(result):
    """Record last check date and result"""
    now = NSDate.new()
    munkicommon.set_pref('LastCheckDate', now)
    munkicommon.set_pref('LastCheckResult', result)


def sendDistrubutedNotification(notification_name, userInfo=None):
    '''Sends a NSDistributedNotification'''
    dnc = NSDistributedNotificationCenter.defaultCenter()
    dnc.postNotificationName_object_userInfo_options_(
        notification_name,
        None,
        userInfo,
        NSNotificationDeliverImmediately + NSNotificationPostToAllSessions)


def sendUpdateNotification():
    '''Sends an update notification via NSDistributedNotificationCenter
    MSU.app registers to receive these events.'''
    userInfo = {'pid': os.getpid()}
    sendDistrubutedNotification(
        'com.googlecode.munki.managedsoftwareupdate.updateschanged',
        userInfo)


def sendDockUpdateNotification():
    '''Sends an update notification via NSDistributedNotificationCenter
    MSU.app's docktileplugin registers to receive these events.'''
    userInfo = {'pid': os.getpid()}
    sendDistrubutedNotification(
        'com.googlecode.munki.managedsoftwareupdate.dock.updateschanged',
        userInfo)


def sendStartNotification():
    '''Sends a start notification via NSDistributedNotificationCenter'''
    userInfo = {'pid': os.getpid()}
    sendDistrubutedNotification(
        'com.googlecode.munki.managedsoftwareupdate.started',
        userInfo)


def sendEndNotification():
    '''Sends an ended notification via NSDistributedNotificationCenter'''
    userInfo = {'pid': os.getpid()}
    sendDistrubutedNotification(
        'com.googlecode.munki.managedsoftwareupdate.ended',
        userInfo)


def notifyUserOfUpdates(force=False):
    """Notify the logged-in user of available updates.

    Args:
      force: bool, default False, forcefully notify user regardless
          of LastNotifiedDate.
    Returns:
      Boolean.  True if the user was notified, False otherwise.
    """
    # called when options.auto == True
    # someone is logged in, and we have updates.
    # if we haven't notified in a while, notify:
    user_was_notified = False
    lastNotifiedString = munkicommon.pref('LastNotifiedDate')
    try:
        daysBetweenNotifications = int(
            munkicommon.pref('DaysBetweenNotifications'))
    except ValueError:
        munkicommon.display_warning(
            'DaysBetweenNotifications is not an integer: %s'
            % munkicommon.pref('DaysBetweenNotifications'))
        # continue with the default DaysBetweenNotifications
        daysBetweenNotifications = 1
    now = NSDate.new()
    nextNotifyDate = now
    if lastNotifiedString:
        lastNotifiedDate = NSDate.dateWithString_(lastNotifiedString)
        interval = daysBetweenNotifications * (24 * 60 * 60)
        if daysBetweenNotifications > 0:
            # we make this adjustment so a 'daily' notification
            # doesn't require 24 hours to elapse
            # subtract 6 hours
            interval = interval - (6 * 60 * 60)
        nextNotifyDate = lastNotifiedDate.dateByAddingTimeInterval_(interval)
    if force or now.timeIntervalSinceDate_(nextNotifyDate) >= 0:
        # record current notification date
        munkicommon.set_pref('LastNotifiedDate', now)

        munkicommon.log('Notifying user of available updates.')
        munkicommon.log('LastNotifiedDate was %s' % lastNotifiedString)

        # notify user of available updates using LaunchAgent to start
        # Managed Software Update.app in the user context.
        launchfile = '/var/run/com.googlecode.munki.ManagedSoftwareUpdate'
        f = open(launchfile, 'w')
        f.close()
        time.sleep(0.5)
        if os.path.exists(launchfile):
            os.unlink(launchfile)
        user_was_notified = True
    return user_was_notified


def warn_if_server_is_default(server):
    '''Munki defaults to using http://munki/repo as the base URL.
    This is useful as a bootstrapping default, but is insecure.
    Warn the admin if Munki is using an insecure default.'''
    # server can be either ManifestURL or SoftwareRepoURL
    if server.rstrip('/') in ['http://munki/repo',
                              'http://munki/repo/manifests']:
        munkicommon.display_warning(
            'Client is configured to use the default repo, which is insecure. '
            'Client could be trivially compromised when off your '
            'organization\'s network. '
            'Consider using a non-default URL, and preferably an https:// URL.')


def main():
    """Main"""
    # install handler for SIGTERM
    signal.signal(signal.SIGTERM, signal_handler)

    # save this for later
    scriptdir = os.path.realpath(os.path.dirname(sys.argv[0]))

    p = optparse.OptionParser()
    p.set_usage("""Usage: %prog [options]""")
    p.add_option('--auto', '-a', action='store_true',
                    help="""Used by launchd LaunchAgent for scheduled runs.
                    No user feedback or intervention. All other options
                    ignored.""")
    p.add_option('--logoutinstall', '-l', action='store_true',
                    help="""Used by launchd LaunchAgent when running at the
                    loginwindow.""")
    p.add_option('--installwithnologout', action='store_true',
                    help="""Used by Managed Software Update.app when user
                            triggers an install without logging out.""")
    p.add_option('--manualcheck', action='store_true',
                    help="""Used by launchd LaunchAgent when checking
                    manually.""")
    p.add_option('--munkistatusoutput', '-m', action='store_true',
                    help="""Uses MunkiStatus.app for progress feedback when
                    installing.""")
    p.add_option('--id', default='',
                    help='Alternate identifier for catalog retreival')
    p.add_option('--quiet', '-q', action='store_true',
                    help="""Quiet mode. Logs messages, but nothing to stdout.
                    --verbose is ignored if --quiet is used.""")
    p.add_option('--verbose', '-v', action='count', default=1,
                    help="""More verbose output. May be specified multiple
                     times.""")
    p.add_option('--checkonly', action='store_true',
                       help="""Check for updates, but don't install them.
                       This is the default behavior.""")
    p.add_option('--installonly', action='store_true',
                       help='Skip checking and install any pending updates.')
    p.add_option('--applesuspkgsonly', action='store_true',
                       help=('Only check/install Apple SUS packages, '
                             'skip Munki packages.'))
    p.add_option('--munkipkgsonly', action='store_true',
                       help=('Only check/install Munki packages, '
                             'skip Apple SUS.'))
    p.add_option('--version', '-V', action='store_true',
                      help='Print the version of the munki tools and exit.')

    options, dummy_arguments = p.parse_args()

    if options.version:
        print munkicommon.get_version()
        exit(0)

    # check to see if we're root
    if os.geteuid() != 0:
        print >> sys.stderr, 'You must run this as root!'
        exit(munkicommon.EXIT_STATUS_ROOT_REQUIRED)

    runtype = 'custom'

    checkandinstallatstartupflag = \
               '/Users/Shared/.com.googlecode.munki.checkandinstallatstartup'
    installatstartupflag = \
               '/Users/Shared/.com.googlecode.munki.installatstartup'
    installatlogoutflag = '/private/tmp/com.googlecode.munki.installatlogout'

    if options.auto:
        # typically invoked by a launch daemon periodically.
        # munkistatusoutput is false for checking, but true for installing
        runtype = 'auto'
        options.munkistatusoutput = False
        options.quiet = True
        options.checkonly = False
        options.installonly = False

    if options.logoutinstall:
        # typically invoked by launchd agent
        # running in the LoginWindow context
        runtype = 'logoutinstall'
        options.munkistatusoutput = True
        options.quiet = True
        options.checkonly = False
        options.installonly = True
        # if we're running at the loginwindow,
        # let's make sure the user triggered
        # the update before logging out, or we triggered it before restarting.
        user_triggered = False
        flagfiles = [checkandinstallatstartupflag,
                     installatstartupflag,
                     installatlogoutflag]
        for filename in flagfiles:
            if os.path.exists(filename):
                user_triggered = True
                if filename == checkandinstallatstartupflag:
                    runtype = 'checkandinstallatstartup'
                    options.installonly = False
                    options.auto = True
                    # HACK: sometimes this runs before the network is up.
                    # we'll attempt to wait up to 60 seconds for the
                    # network interfaces to come up
                    # before continuing
                    munkicommon.display_status_minor('Waiting for network...')
                    for dummy_i in range(60):
                        if networkUp():
                            break
                        time.sleep(1)
                else:
                    # delete triggerfile if _not_ checkandinstallatstartup
                    os.unlink(filename)
        if not user_triggered:
            # no trigger file was found -- how'd we get launched?
            munkicommon.cleanUpTmpDir()
            exit(0)

    if options.installwithnologout:
        # typically invoked by Managed Software Update.app
        # by user who decides not to logout
        launchdtriggerfile = \
            '/private/tmp/.com.googlecode.munki.managedinstall.launchd'
        if os.path.exists(launchdtriggerfile):
            # remove it so we aren't automatically relaunched
            os.unlink(launchdtriggerfile)
        runtype = 'installwithnologout'
        options.munkistatusoutput = True
        options.quiet = True
        options.checkonly = False
        options.installonly = True

    if options.manualcheck:
        # triggered by Managed Software Update.app
        launchdtriggerfile = \
            '/private/tmp/.com.googlecode.munki.updatecheck.launchd'
        if os.path.exists(launchdtriggerfile):
            try:
                launch_options = FoundationPlist.readPlist(launchdtriggerfile)
                options.munkipkgsonly =  launch_options.get(
                    'SuppressAppleUpdateCheck')
            except FoundationPlist.FoundationPlistException:
                pass
            # remove it so we aren't automatically relaunched
            os.unlink(launchdtriggerfile)
        runtype = 'manualcheck'
        options.munkistatusoutput = True
        options.quiet = True
        options.checkonly = True
        options.installonly = False

    if options.quiet:
        options.verbose = 0

    if options.checkonly and options.installonly:
        print >> sys.stderr, \
              '--checkonly and --installonly options are mutually exclusive!'
        exit(munkicommon.EXIT_STATUS_INVALID_PARAMETERS)

    # set munkicommon globals
    munkicommon.munkistatusoutput = options.munkistatusoutput
    munkicommon.verbose = options.verbose

    # Set environment variable for verbosity
    os.environ['MUNKI_VERBOSITY_LEVEL'] = str(options.verbose)

    if options.installonly:
        # we're only installing, not checking, so we should copy
        # some report values from the prior run
        munkicommon.readreport()

    # start a new report
    munkicommon.report['StartTime'] = munkicommon.format_time()
    munkicommon.report['RunType'] = runtype
    # Clearing arrays must be run before any call to display_warning/error.
    munkicommon.report['Errors'] = []
    munkicommon.report['Warnings'] = []

    if munkicommon.pref('LogToSyslog'):
        munkicommon.configure_syslog()

    munkicommon.log("### Starting managedsoftwareupdate run: %s ###" % runtype)
    if options.verbose:
        print 'Managed Software Update Tool'
        print 'Version %s' % munkicommon.get_version()
        print 'Copyright 2010-2015 The Munki Project'
        print 'https://github.com/munki/munki\n'

    munkicommon.display_status_major('Starting...')
    sendStartNotification()
    # run the preflight script if it exists
    preflightscript = os.path.join(scriptdir, 'preflight')
    result = runScript(preflightscript, 'preflight', runtype)

    if result:
        # non-zero return code means don't run
        munkicommon.display_info(
            'managedsoftwareupdate run aborted by preflight script: %s'
            % result)
        # record the check result for use by Managed Software Update.app
        # right now, we'll return the same code as if the munki server
        # was unavailable. We need to revisit this and define additional
        # update check results.
        recordUpdateCheckResult(-2)
        if options.munkistatusoutput:
            # connect to socket and quit
            munkistatus.activate()
            munkistatus.quit()
        munkicommon.cleanUpTmpDir()
        exit(result)
    # Force a prefs refresh, in case preflight modified the prefs file.
    munkicommon.reload_prefs()

    # create needed directories if necessary
    if not initMunkiDirs():
        exit(munkicommon.EXIT_STATUS_MUNKI_DIRS_FAILURE)

    # check to see if another instance of this script is running
    if munkicommon.managedsoftwareupdate_running():
        # another instance of this script is running, so we should quit
        if options.manualcheck:
            # a manual update check was triggered
            # (probably by Managed Software Update), but managedsoftwareupdate
            # is already running. We should provide user feedback
            munkistatus.activate()
            munkistatus.message('Checking for available updates...')
            while True:
                # loop til the other instance exits
                if not munkicommon.managedsoftwareupdate_running():
                    break
                # or user clicks Stop
                if munkicommon.stopRequested():
                    break
                time.sleep(0.5)

            munkistatus.quit()
        else:
            myname = os.path.basename(sys.argv[0])
            msg = 'Another instance of %s is running. Exiting.' % myname
            munkicommon.log(msg)
            print >> sys.stderr, msg
        munkicommon.cleanUpTmpDir()
        exit(0)

    applesoftwareupdatesonly = (munkicommon.pref('AppleSoftwareUpdatesOnly')
        or options.applesuspkgsonly)

    skip_munki_check = (options.installonly or applesoftwareupdatesonly)
    if not skip_munki_check:
        # check to see if we can talk to the manifest server
        server = munkicommon.pref('ManifestURL') or \
                 munkicommon.pref('SoftwareRepoURL')
        warn_if_server_is_default(server)
        result = updatecheck.checkServer(server)
        if result != (0, 'OK'):
            munkicommon.display_error(
                'managedsoftwareupdate: server check for %s failed: %s'
                % (server, str(result)))
            if options.manualcheck:
                # record our result
                recordUpdateCheckResult(-1)
                # connect to socket and quit
                munkistatus.activate()
                munkistatus.quit()
            if not options.auto:
                munkicommon.cleanUpTmpDir()
                exit(munkicommon.EXIT_STATUS_SERVER_UNAVAILABLE)
            else:
                # even if we can't reach the manifest server, we can
                # attempt to do installs of cached items. Setting
                # skip_munki_check to True will cause the updatecheck
                # to be skipped
                skip_munki_check = True

    # reset our errors and warnings files, rotate main log if needed
    munkicommon.reset_errors()
    munkicommon.reset_warnings()
    munkicommon.rotate_main_log()

    # archive the previous session's report
    munkicommon.archive_report()

    if applesoftwareupdatesonly and options.verbose:
        print ('NOTE: managedsoftwareupdate is configured to process Apple '
               'Software Updates only.')

    updatecheckresult = None
    if not skip_munki_check:
        try:
            updatecheckresult = updatecheck.check(client_id=options.id)
        except KeyboardInterrupt:
            munkicommon.savereport()
            raise
        except:
            munkicommon.display_error('Unexpected error in updatecheck:')
            munkicommon.log(traceback.format_exc())
            munkicommon.savereport()
            raise

    if updatecheckresult is not None:
        recordUpdateCheckResult(updatecheckresult)

    updatesavailable = munkiUpdatesAvailable()
    appleupdatesavailable = 0

    # should we do Apple Software updates this run?
    if applesoftwareupdatesonly:
        # admin told us to only do Apple updates this run
        should_do_apple_updates = True
    elif options.munkipkgsonly:
        # admin told us to skip Apple updates for this run
        should_do_apple_updates = False
    elif munkiUpdatesContainAppleItems():
        # shouldn't run Software Update if we're doing Apple items
        # with Munki items
        should_do_apple_updates = False
        # if there are force_install_after_date items in a pre-existing
        # AppleUpdates.plist this means we are blocking those updates.
        # we need to delete AppleUpdates.plist so that other code doesn't
        # mistakenly alert for forced installs it isn't actually going to
        # install.
        appleupdates_plist = os.path.join(
            munkicommon.pref('ManagedInstallDir'), 'AppleUpdates.plist')
        try:
            os.unlink(appleupdates_plist)
        except OSError:
            pass

    else:
        # check the normal preferences
        should_do_apple_updates = munkicommon.pref(
                                        'InstallAppleSoftwareUpdates')

    if should_do_apple_updates:
        if (not options.installonly and not munkicommon.stopRequested()):
            force_update_check = False
            force_catalog_refresh = False
            if (options.manualcheck or runtype == 'checkandinstallatstartup'):
                force_update_check = True
            if (runtype == 'custom' and applesoftwareupdatesonly):
                force_update_check = True
                force_catalog_refresh = True
            try:
                appleupdatesavailable = \
                    appleupdates.appleSoftwareUpdatesAvailable(
                        forcecheck=force_update_check, client_id=options.id,
                        forcecatalogrefresh=force_catalog_refresh)
            except KeyboardInterrupt:
                munkicommon.savereport()
                raise
            except:
                munkicommon.display_error('Unexpected error in appleupdates:')
                munkicommon.log(traceback.format_exc())
                munkicommon.savereport()
                raise
            if applesoftwareupdatesonly:
                # normally we record the result of checking for Munki updates
                # but if we are only doing Apple updates, we should record the
                # result of the Apple updates check
                if appleupdatesavailable:
                    recordUpdateCheckResult(1)
                else:
                    recordUpdateCheckResult(0)

        if options.installonly:
            # just look and see if there are already downloaded Apple updates
            # to install; don't run softwareupdate to check with Apple
            try:
                appleupdatesavailable = \
                    appleupdates.appleSoftwareUpdatesAvailable(
                        suppresscheck=True, client_id=options.id)
            except KeyboardInterrupt:
                munkicommon.savereport()
                raise
            except:
                munkicommon.display_error('Unexpected error in appleupdates:')
                munkicommon.log(traceback.format_exc())
                munkicommon.savereport()
                raise

    # display any available update information
    if updatecheckresult:
        updatecheck.displayUpdateInfo()
    if appleupdatesavailable:
        appleupdates.displayAppleUpdateInfo()

    # send a notification event so MSU can update its display
    # if needed
    sendUpdateNotification()

    mustrestart = False
    mustlogout = False
    notify_user = False
    force_action = None
    if updatesavailable or appleupdatesavailable:
        if options.installonly or options.logoutinstall:
            # just install
            mustrestart = doInstallTasks(appleupdatesavailable)
            # reset our count of available updates (it might not actually
            # be zero, but we want to clear the badge on the Dock icon;
            # it can be updated to the "real" count on the next Munki run)
            updatesavailable = 0
            appleupdatesavailable = 0
            # send a notification event so MSU can update its display
            # if needed
            sendUpdateNotification()

        elif options.auto:
            if not munkicommon.currentGUIusers():  # no GUI users
                if munkicommon.pref('SuppressAutoInstall'):
                    # admin says we can never install packages
                    # without user approval/initiation
                    munkicommon.log('Skipping auto install because '
                                    'SuppressAutoInstall is true.')
                elif munkicommon.pref('SuppressLoginwindowInstall'):
                    # admin says we can't install pkgs at loginwindow
                    # unless they don't require a logout or restart
                    # (and are marked with unattended_install = True)
                    #
                    # check for packages that need to be force installed
                    # soon and convert them to unattended_installs if they
                    # don't require a logout
                    dummy_action = updatecheck.checkForceInstallPackages()
                    # now install anything that can be done unattended
                    munkicommon.log('Installing only items marked unattended '
                                    'because SuppressLoginwindowInstall is '
                                    'true.')
                    ignore_restart = doInstallTasks(
                        appleupdatesavailable, only_unattended=True)
                elif getIdleSeconds() < 10:
                    munkicommon.log('Skipping auto install at loginwindow '
                                    'because system is not idle '
                                    '(keyboard or mouse activity).')
                elif munkicommon.isAppRunning(
                    '/System/Library/CoreServices/FileSyncAgent.app'):
                    munkicommon.log('Skipping auto install at loginwindow '
                                    'because FileSyncAgent.app is running '
                                    '(HomeSyncing a mobile account on login?).')
                else:
                    # no GUI users, system is idle, so we can install
                    # but first, enable status output over login window
                    munkicommon.munkistatusoutput = True
                    munkicommon.log('No GUI users, installing at login window.')
                    munkistatus.launchMunkiStatus()
                    mustrestart = doInstallTasks(appleupdatesavailable)
                    # reset our count of available updates 
                    updatesavailable = 0
                    appleupdatesavailable = 0
            else:  # there are GUI users
                if munkicommon.pref('SuppressAutoInstall'):
                    munkicommon.log('Skipping unattended installs because '
                                     'SuppressAutoInstall is true.')
                else:
                    # check for packages that need to be force installed
                    # soon and convert them to unattended_installs if they
                    # don't require a logout
                    dummy_action = updatecheck.checkForceInstallPackages()
                    # install anything that can be done unattended
                    ignore_restart = doInstallTasks(
                        appleupdatesavailable, only_unattended=True)

                # send a notification event so MSU can update its display
                # if needed
                sendUpdateNotification()

                force_action = updatecheck.checkForceInstallPackages()
                # if any installs are still requiring force actions, just
                # initiate a logout to get started.  blocking apps might
                # have stopped even non-logout/reboot installs from
                # occuring.
                if force_action in ['now', 'logout', 'restart']:
                    mustlogout = True

                # it's possible that we no longer have any available updates
                # so we need to check InstallInfo.plist and
                # AppleUpdates.plist again
                updatesavailable = munkiUpdatesAvailable()
                try:
                    appleupdatesavailable = \
                        appleupdates.appleSoftwareUpdatesAvailable(
                            suppresscheck=True, client_id=options.id)
                except KeyboardInterrupt:
                    munkicommon.savereport()
                    raise
                except:
                    munkicommon.display_error(
                        'Unexpected error in appleupdates:')
                    munkicommon.log(traceback.format_exc())
                    munkicommon.savereport()
                    raise
                if appleupdatesavailable or updatesavailable:
                    # set a flag to notify the user of available updates
                    # after we conclude this run.
                    notify_user = True

        elif not options.quiet:
            print ('\nRun %s --installonly to install the downloaded '
                   'updates.' % myname)
    else:
        # no updates available
        if options.installonly and not options.quiet:
            print 'Nothing to install or remove.'
        if runtype == 'checkandinstallatstartup':
            # we have nothing to do, so remove the
            # checkandinstallatstartupflag file
            # so we'll stop running at startup/logout
            if os.path.exists(checkandinstallatstartupflag):
                os.unlink(checkandinstallatstartupflag)

    # finish our report
    munkicommon.report['EndTime'] = munkicommon.format_time()
    munkicommon.report['ManagedInstallVersion'] = munkicommon.get_version()
    munkicommon.report['AvailableDiskSpace'] = \
                                        munkicommon.getAvailableDiskSpace()
    munkicommon.report['ConsoleUser'] = munkicommon.getconsoleuser() or \
                                        '<None>'
    munkicommon.savereport()

    # store the current pending update count
    munkicommon.set_pref('PendingUpdateCount',
                         updatesavailable + appleupdatesavailable)

    # send a notification event so Dock tile badge can be updated
    # if needed
    sendDockUpdateNotification()

    munkicommon.display_status_major('Finishing...')
    sendEndNotification()
    # save application inventory data
    munkicommon.saveappdata()
    # run the postflight script if it exists
    postflightscript = os.path.join(scriptdir, 'postflight')
    result = runScript(postflightscript, 'postflight', runtype)
    # we ignore the result of the postflight

    munkicommon.log("### Ending managedsoftwareupdate run ###")
    if options.verbose:
        print 'Done.'

    if notify_user:
        # it may have been more than a minute since we ran our
        # original updatecheck so tickle the updatecheck time
        # so MSU.app knows to display results immediately
        recordUpdateCheckResult(1)
        if force_action:
            notifyUserOfUpdates(force=True)
            time.sleep(2)
            startLogoutHelper()
        elif munkicommon.getconsoleuser() == u'loginwindow':
            # someone is logged in, but we're sitting at
            # the loginwindow due to fast user switching
            # so do nothing
            pass
        elif not munkicommon.pref('SuppressUserNotification'):
            notifyUserOfUpdates()
        else:
            munkicommon.log('Skipping user notification because '
                            'SuppressUserNotification is true.')

    munkicommon.cleanUpTmpDir()
    if mustlogout:
        # not handling this currently
        pass
    if mustrestart:
        doRestart()
    elif munkicommon.munkistatusoutput:
        munkistatus.quit()

    if runtype == 'checkandinstallatstartup' and not mustrestart:
        if os.path.exists(checkandinstallatstartupflag):
            # we installed things but did not need to restart; we need to run
            # again to check for more updates.
            if not munkicommon.currentGUIusers():
                # no-one is logged in
                idleseconds = getIdleSeconds()
                if not idleseconds > 10:
                    # system is not idle, but check again in case someone has
                    # simply briefly touched the mouse to see progress.
                    time.sleep(15)
                    idleseconds = getIdleSeconds()
                if idleseconds > 10:
                    # no-one is logged in and the machine has been idle
                    # for a few seconds; kill the loginwindow
                    # (which will cause us to run again)
                    #munkicommon.log(
                    #    'Killing loginwindow so we will run again...')
                    #cmd = ['/usr/bin/killall', 'loginwindow']
                    #dummy_retcode = subprocess.call(cmd)
                    # with the new LaunchAgent, we don't have to kill
                    # the loginwindow
                    pass
                else:
                    # if the trigger file is present when we exit, we'll
                    # be relaunched by launchd, so we need to remove it
                    # to prevent automatic relaunch.
                    munkicommon.log(
                        'System not idle -- '
                        'removing trigger file to prevent relaunch')
                    try:
                        os.unlink(checkandinstallatstartupflag)
                    except OSError:
                        pass


if __name__ == '__main__':
    main()
