#!/usr/bin/python
# -*- coding: utf-8; mode: python; tab-width: 3; indent-tabs-mode: nil -*-
#
# Copyright 2012, 2013, 2014, 2015
# Raffaello D. Di Napoli
#
# This file is part of kernel-tools.
#
# kernel-tools is free software: you can redistribute it and/or modify it under the terms of the GNU
# General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# kernel-tools is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
# even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with kernel-tools. If not,
# see <http://www.gnu.org/licenses/>.
#---------------------------------------------------------------------------------------------------

"""Implementation of the class Generator."""

import glob
import os
import portage.package.ebuild.config as portage_config
import re
import shlex
import shutil
import subprocess
import sys
from . import OutOfTreeEnumerator

####################################################################################################
# Compressor

class Compressor(object):
   """Stores information about an external compressor program."""

   def __init__(self, sConfigName, sExt, iterCmdArgs):
      """Constructor.

      str sConfig
         Name of the compressor as per Linux’s .config file.
      str sExt
         Default file name extension for files compressed by this program.
      iterable(str*) iterCmdArgs
         Command-line arguments to use to run the compressor.
      """

      self._m_iterCmdArgs = iterCmdArgs
      self._m_sConfigName = sConfigName
      self._m_sExt = sExt

   def cmd_args(self):
      """Returns the command-line arguments to use to run the compressor.

      iterable(str*) return
         Command-line arguments.
      """

      return self._m_iterCmdArgs

   def config_name(self):
      """Returns the name of the compressor as per Linux’s .config file.

      str return
         Compressor name.
      """

      return self._m_sConfigName

   def file_name_ext(self):
      """Returns the default file name extension for files compressed by this program.

      str return
         File name extension, including the dot.
      """

      return self._m_sExt

####################################################################################################
# GeneratorError

class GeneratorError(Exception):
   """Indicates a failure in the generation of a kernel binary package."""

   pass

####################################################################################################
# Generator

class Generator(object):
   """Generates a Portage binary package containing a kernel image and related in-tree kernel
   modules, optionally generating an initramfs from a compatible self-contained initramfs-building
   system such as Tinytium.
   """

   # List of supported compressors, in order of preference.
   _smc_listCompressors = [
      Compressor('LZO',   '.lzo' , ('lzop',  '-9')),
      Compressor('LZMA',  '.lzma', ('lzma',  '-9')),
      Compressor('BZIP2', '.bz2' , ('bzip2', '-9')),
      Compressor('GZIP',  '.gz'  , ('gzip',  '-9')),
   ]
   # ebuild template that will be dropped in the selected overlay and made into a binary package.
   _smc_sEbuildTemplate = '''
      EAPI=5

      SLOT="${PVR}"
      DESCRIPTION="Linux kernel image and modules"
      HOMEPAGE="http://www.kernel.org"
      LICENSE="GPL-2"

      inherit mount-boot

      KEYWORDS="${ARCH}"
      # Avoid stripping kernel binaries.
      RESTRICT="strip"

      S="${WORKDIR}"

      src_install() {
         echo "KERNEL-GEN: D=${D}"
      }
   '''.replace('\n      ', '\n').rstrip(' ')
   # Special cases for the conversion from Portage ARCH to Linux ARCH.
   _smc_dictPArchToKArch = {
      'amd64': 'x86_64',
      'arm64': 'aarch64',
      'm68k' : 'm68',
      'ppc'  : 'powerpc',
      'ppc64': 'powerpc64',
      'x86'  : 'i386',
   }

   def __init__(self, sPArch, sIrfSourcePath, sRoot, sSourcePath):
      """Constructor. TODO: comment"""

      if sRoot:
         # Set this now to override Portage’s default root.
         os.environ['ROOT'] = sRoot
      self._m_pconfig = portage_config.config()
      if not sRoot:
         # Set this now to override the null sRoot with Portage’s default root.
         os.environ['ROOT'] = sRoot = self._m_pconfig['ROOT']
      self._m_sCrossCompiler = None
      self._m_sEbuildFilePath = None
      self._m_sEbuildPkgRoot = None
      self._m_fileNullOut = open(os.devnull, 'w')
      self._m_sIndent = ''
      self._m_comprIrf = None
      self._m_sIrfSourcePath = sIrfSourcePath
      self._m_listKMakeArgs = ['make']
      self._m_listKMakeArgs.extend(shlex.split(self._m_pconfig['MAKEOPTS']))
      self._m_dictKMakeEnv = dict(os.environ)
      if not sPArch:
         sPArch = self._m_pconfig['ARCH']
      self._m_dictKMakeEnv['ARCH'] = self._smc_dictPArchToKArch.get(sPArch, sPArch)
      self._m_tplModulePackages = None
      self._m_sRoot = sRoot
      self._m_sSourcePath = sSourcePath
      self._m_sSrcConfigPath = None
      self._m_sSrcImagePath = None
      self._m_sIrfArchivePath = None
      self._m_sTmpDir = self._m_pconfig['PORTAGE_TMPDIR']

   def __del__(self):
      """Destructor."""

      if self._m_sEbuildFilePath:
         self.einfo('Cleaning up package build temporary directory')
         with subprocess.Popen(
            ('ebuild', self._m_sEbuildFilePath, 'clean'),
            stdout = self._m_fileNullOut, stderr = subprocess.STDOUT
         ) as procClean:
            procClean.communicate()
         os.unlink(self._m_sEbuildFilePath)
         # TODO: delete the ebuild directory if now it only contains the manifest file.

      self._m_fileNullOut.close()

   def build_initramfs(self, bDebug = False):
      """Builds an initramfs for the kernel generated by build_kernel().

      bool bDebug
         If True, the contents of the generated initramfs will be dumped to a file for later
         inspection.
      """

      self.einfo('Generating initramfs')
      self.eindent()

      sPrevDir = os.getcwd()
      sIrfWorkDir = os.path.join(self._m_sTmpDir, 'initramfs-' + self._m_sKernelRelease)
      shutil.rmtree(sIrfWorkDir, ignore_errors = True)
      os.mkdir(sIrfWorkDir)
      try:
         os.chdir(sIrfWorkDir)

         self.einfo('Adding kernel modules')
         self.kmake_check_call('INSTALL_MOD_PATH=' + sIrfWorkDir, 'modules_install')
         # TODO: configuration-driven exclusion of modules from the initramfs.
         setExcludedModDirs = set([
            'arch/x86/kvm',
            'drivers/bluetooth',
            'drivers/media',
            'net/bluetooth',
            'net/netfilter',
            'sound',
            'vhost',
         ])
         # Equivalent to executing:
         #    rm -rf sIrfWorkDir/lib*/modules/*/kernel/{${setExcludedModDirs}}
         for sDir in os.listdir(sIrfWorkDir):
            if sDir.startswith('lib'):
               sModulesDir = os.path.join(sIrfWorkDir, sDir, 'modules')
               for sDir in os.listdir(sModulesDir):
                  sKernelModulesDir = os.path.join(sModulesDir, sDir, 'kernel')
                  for sDir in setExcludedModDirs:
                     sDir = os.path.join(sKernelModulesDir, sDir)
                     # Recursively remove the excluded directory.
                     shutil.rmtree(sDir, ignore_errors = True)

         self.einfo('Adding out-of-tree firmware')
         # Create the folder beforehand; it not needed, we'll delete it later.
         sSrcFirmwareDir = os.path.join(self._m_sRoot, 'lib/firmware')
         sDstFirmwareDir = os.path.join(sIrfWorkDir, 'lib/firmware')
         oote = OutOfTreeEnumerator(bFirmware = True, bModules = False)
         for sSrcExtFirmwarePath in oote.files():
            sDstExtFirmwarePath = os.path.join(sDstFirmwareDir, sSrcExtFirmwarePath)
            os.makedirs(os.path.dirname(sDstExtFirmwarePath), exist_ok = True)
            # Copy the firmware file.
            shutil.copy2(os.path.join(sSrcFirmwareDir, sSrcExtFirmwarePath), sDstExtFirmwarePath)

         sIrfBuild = os.path.join(self._m_sIrfSourcePath, 'build')
         if os.path.isfile(sIrfBuild) and os.access(sIrfBuild, os.R_OK | os.X_OK):
            # The initramfs has a build script; invoke it.
            self.einfo('Invoking initramfs custom build script')
            self.eindent()
            dictIrfBuildEnv = dict(os.environ)
            dictIrfBuildEnv['ARCH'] = self._m_dictKMakeEnv['ARCH']
            if self._m_sCrossCompiler:
               dictIrfBuildEnv['CROSS_COMPILE'] = self._m_sCrossCompiler
            dictIrfBuildEnv['PORTAGE_ARCH'] = self._m_pconfig['ARCH']
            try:
               subprocess.check_call((sIrfBuild, ), env = dictIrfBuildEnv)
            finally:
               self.eoutdent()
            del dictIrfBuildEnv
         else:
            # No build script; just copy every file.
            self.einfo('Adding source files')
            for sIrfFile in os.listdir(self._m_sIrfSourcePath):
               shutil.copytree(os.path.join(self._m_sIrfSourcePath, sIrfFile), sIrfWorkDir)

         # Build a list with every file name for cpio to package, relative to the current directory
         # (sIrfWorkDir).
         self.einfo('Collecting file names')
         listIrfContents = []
         cchIrfWorkDir = len(sIrfWorkDir) + 1
         for sBaseDir, _, listFileNames in os.walk(sIrfWorkDir):
            # Strip the work directory, changing sIrfWorkDir into ‘.’.
            sBaseDir = sBaseDir[cchIrfWorkDir:]
            if sBaseDir:
               sBaseDir += '/'
            for sFileName in listFileNames:
               listIrfContents.append(sBaseDir + sFileName)
         if bDebug:
            sIrfDumpFileName = os.path.join(
               os.environ.get('TMPDIR', '/tmp'), 'initramfs-' + self._m_sKernelRelease + '.ls'
            )
            with open(sIrfDumpFileName, 'w') as fileIrfDump:
               self.einfo('Dumping contents of generated initramfs to {}'.format(sIrfDumpFileName))
               subprocess.check_call(
                  ['ls', '-lR', '--color=always'] + listIrfContents,
                  stdout = fileIrfDump, universal_newlines = True
               )
#         byCpioInput = b'\0'.join(bytes(sPath, encoding = 'utf-8') for sPath in listIrfContents)
         del listIrfContents

         self.einfo('Creating archive')
         with open(self._m_sIrfArchivePath, 'wb') as fileIrfArchive:
            # Spawn the compressor or just a cat.
            if self._m_comprIrf:
               tplCompressorArgs = self._m_comprIrf.cmd_args()
            else:
               tplCompressorArgs = ('cat', )
            with subprocess.Popen(
               tplCompressorArgs, stdin = subprocess.PIPE, stdout = fileIrfArchive
            ) as procCompress:
               # Make cpio write to the compressor’s input, and redirect its stderr to /dev/null
               # since it likes to output junk.
               with subprocess.Popen(
                  ('cpio', '--create', '--format=newc', '--null', '--owner=0:0'),
                  stdin = subprocess.PIPE, stdout = procCompress.stdin, stderr = self._m_fileNullOut
               ) as procCpio:
#                  # Send cpio the list of files to package.
#                  procCpio.communicate(byCpioInput)
                  # Use find . to enumerate the files for cpio to pack.
                  with subprocess.Popen(
                     ('find', '.', '-print0'), stdout = procCpio.stdin
                  ) as procFind:
                     procFind.communicate()
                  procCpio.communicate()
               procCompress.communicate()
      finally:
         self.einfo('Cleaning up initramfs')
         os.chdir(sPrevDir)
         shutil.rmtree(sIrfWorkDir)

      self.eoutdent()

   def build_kernel(self, bRebuildOutOfTreeModules = True):
      """Builds the kernel image and modules.

      bool bRebuildOutOfTreeModules
         If True, packages that install out-of-tree modules will be rebuilt in order to ensure
         binary compatibility with the kernel being built.
      """

      self.einfo('Ready to build:')
      self.eindent()
      self.einfo('\033[1;32mlinux-{}\033[0m ({})'.format(
         self._m_sKernelRelease, self._m_dictKMakeEnv['ARCH']
      ))
      self.einfo('from \033[1;37m{}\033[0m'.format(self._m_sSourcePath))

      if self._m_sIrfSourcePath:
         # Check that a valid initramfs directory was specified.
         self._m_sIrfSourcePath = os.path.realpath(self._m_sIrfSourcePath)
         self.einfo('with initramfs from \033[1;37m{}\033[0m'.format(self._m_sIrfSourcePath))
      if self._m_sCrossCompiler:
         self.einfo('cross-compiled with \033[1;37m{}\033[0m toolchain'.format(
            self._m_sCrossCompiler
         ))
      self.eoutdent()

      # Use distcc, if enabled.
      # TODO: also add HOSTCC.
      if 'distcc' in self._m_pconfig.features:
         self.einfo('Distributed C compiler (distcc) enabled')
         self._m_listKMakeArgs.append('CC=distcc')
         sDistCCDir = os.path.join(self._m_pconfig['PORTAGE_TMPDIR'], 'portage/.distcc')
         iOldMask = os.umask(0o002)
         os.makedirs(sDistCCDir, exist_ok = True)
         os.umask(iOldMask)
         self._m_dictKMakeEnv['DISTCC_DIR'] = sDistCCDir

      # Only invoke make if .config was changed since last compilation.
      # Note that this check only works due to what we’ll do after invoking kmake (see below, at the
      # end of the if block), because kmake won’t touch the kernel image if .config doesn’t require
      # so, which means that .config can be still more recent than the image even after kmake
      # completes, and this would cause this if branch to be always entered.
      if not os.path.exists(self._m_sSrcImagePath) or \
         os.path.getmtime(self._m_sSrcConfigPath) > os.path.getmtime(self._m_sSrcImagePath) \
      :
         if bRebuildOutOfTreeModules:
            self.einfo('Preparing to rebuild out-of-tree kernel modules')
            self.kmake_check_call('modules_prepare')
            self.einfo('Finished building linux-{}'.format(self._m_sKernelRelease))

            self.einfo('Getting a list of out-of-tree kernel modules')
            oote = OutOfTreeEnumerator(bFirmware = False, bModules = True)
            self._m_tplModulePackages = tuple(oote.packages())
            if self._m_tplModulePackages:
               self.einfo('Rebuilding out-of-tree kernel modules\' packages')
               # First make sure that all the modules’ dependencies are installed.
               self.emerge_check_call(
                  '--changed-use', '--onlydeps', '--update', *self._m_tplModulePackages
               )
               # Then (re)build the modules, but only generate their binary packages.
               dictEmergeEnv = dict(os.environ)
               dictEmergeEnv['KERNEL_DIR'] = self._m_sSourcePath
               self.emerge_check_call(
                  '--buildpkgonly', '--usepkg=n', *self._m_tplModulePackages,
                  dictEnv = dictEmergeEnv
               )
               del dictEmergeEnv

         self.einfo('Building kernel image and in-tree modules')
         self.kmake_check_call()

         # Touch the kernel image now, to avoid always re-running kmake (see large comment above).
         os.utime(self._m_sSrcImagePath, None)

   def create_ebuild(self, sOverlayName = None):
      """Creates the temporary ebuild from which a binary package will be created later.

      str sOverlayName
         Name of ther overlay in which the package ebuild will be added; defaults to the overlay
         with the highest priority.
      """

      self.make_package_name()

      # Get the specified overlay or the one with the highest priority.
      if sOverlayName is None:
         sOverlayName = self._m_pconfig.repositories.prepos_order[-1]
      povl = self._m_pconfig.repositories.prepos.get(sOverlayName)
      if not povl:
         self.eerror('Unknown overlay: {}'.format(sOverlayName))
         raise GeneratorError()
      self.einfo('Creating temporary ebuild \033[1;32m{}/{}-{}::{}\033[0m'.format(
         self._m_sCategory, self._m_sPackageName, self._m_sPackageVersion, sOverlayName
      ))
      # Generate a new ebuild at the expected location in the selected overlay.
      sEbuildFileDir = os.path.join(povl.location, self._m_sCategory, self._m_sPackageName)
      os.makedirs(sEbuildFileDir, exist_ok = True)
      self._m_sEbuildFilePath = os.path.join(
         sEbuildFileDir, '{}-{}.ebuild'.format(self._m_sPackageName, self._m_sPackageVersion)
      )
      with open(self._m_sEbuildFilePath, 'wt') as fileEbuild:
         fileEbuild.write(self._smc_sEbuildTemplate)

      # Have Portage create the package installation image for the ebuild. The ebuild will output
      # the destination path, ${D}, using a pattern specific to kernel-gen.
      sOut = subprocess.check_output(
         ('ebuild', self._m_sEbuildFilePath, 'clean', 'manifest', 'install'),
         stderr = subprocess.STDOUT, universal_newlines = True
      )
      match = re.search(r'^KERNEL-GEN: D=(?P<D>.*)$', sOut, re.MULTILINE)
      self._m_sEbuildPkgRoot = match.group('D')

   def eerror(self, s):
      """TODO: comment"""

      print(self._m_sIndent + '[E] ' + s)

   def eindent(self):
      """TODO: comment"""

      self._m_sIndent += '  '

   def einfo(self, s):
      """TODO: comment"""

      print(self._m_sIndent + '[I] ' + s)

   def emerge_check_call(self, *iterArgs, dictEnv = None):
      """Invokes emerge in “quiet” mode with the specified additional command-line arguments.

      iterable(str*) iterArgs
         Additional arguments to pass to emerge.
      dict(str: str) dictEnv
         Optional environment variable dictionary to use in place of os.environ.
      """

      listArgs = [(self._m_sCrossCompiler or '') + 'emerge']
      bVerbose = False
      if bVerbose:
         listArgs.append('--verbose')
      else:
         listArgs.extend(('--quiet', '--quiet-build', '--quiet-fail=y'))
      listArgs.extend(iterArgs)
      subprocess.check_call(
         listArgs, env = dictEnv, stdout = None if bVerbose else self._m_fileNullOut
      )

   def eoutdent(self):
      """TODO: comment"""

      self._m_sIndent = self._m_sIndent[:-2]

   def ewarn(self, s):
      """TODO: comment"""

      print(self._m_sIndent + '[W] ' + s)

   def install(self):
      """Installs the generated kernel binary package."""

      self.einfo('Installing kernel binary package \033[1;35m{}/{}-{}\033[0m'.format(
         self._m_sCategory, self._m_sPackageName, self._m_sPackageVersion
      ))
      self.emerge_check_call('--select', '--usepkgonly=y', '={}/{}-{}'.format(
         self._m_sCategory, self._m_sPackageName, self._m_sPackageVersion
      ))
      if self._m_tplModulePackages:
         self.einfo('Installing out-of-tree kernel modules\' binary packages')
         self.emerge_check_call('--oneshot', '--usepkgonly=y', *self._m_tplModulePackages)

   def kmake_call_kernelversion(self):
      """Retrieves the kernel version for the source directory specified in the constructor.

      str return
         Kernel version reported by “make kernelversion”.
      """

      # Ignore errors; if no source directory can be found, we’ll take care of failing.
      with subprocess.Popen(
         self._m_listKMakeArgs + ['--directory', self._m_sSourcePath, '--quiet', 'kernelversion'],
         env = self._m_dictKMakeEnv, stdout = subprocess.PIPE, stderr = self._m_fileNullOut,
         universal_newlines = True
      ) as procMake:
         sOut = procMake.communicate()[0].rstrip()
         # Expect a single line; if multiple lines are present, they must be errors.
         if procMake.returncode == 0 and '\n' not in sOut:
            return sOut
      return None

   def kmake_check_call(self, *iterArgs):
      """Invokes kmake with the specified additional command-line arguments.

      iterable(str*) iterArgs
         Additional arguments to pass to kmake.
      """

      listArgs = list(self._m_listKMakeArgs)
      listArgs.append('--quiet')
      listArgs.extend(iterArgs)
      subprocess.check_output(listArgs, env = self._m_dictKMakeEnv, stderr = self._m_fileNullOut)

   def kmake_check_output(self, sTarget):
      """Runs kmake to build the specified informative target, such as “kernelrelease”.

      str sTarget
         Target to “build”.
      str return
         Output of kmake.
      """

      sOut = subprocess.check_output(
         self._m_listKMakeArgs + ['--quiet', sTarget],
         env = self._m_dictKMakeEnv, stderr = subprocess.STDOUT, universal_newlines = True
      ).rstrip()
      if '\n' in sOut:
         self.eerror('Unexpected output by make {}:'.format(sTarget))
         self.eerror(sOut)
         raise GeneratorError()
      return sOut

   def load_kernel_config(self):
      """Loads the selected kernel configuration file (.config), storing the entries defined in it
      and verifying that it’s for the correct kernel version.
      """

      dictKernelConfig = {}
      with open(self._m_sSrcConfigPath, 'r') as fileConfig:
         bConfigVersionFound = False
         for iLine, sLine in enumerate(fileConfig, start = 1):
            sLine = sLine.rstrip()
            if not bConfigVersionFound:
               # In the first 5 lines, expect to find a line that indicates the kernel has already
               # been configured.
               if iLine < 5:
                  # Match: “Linux/i386 2.6.37 Kernel Configuration”.
                  match = re.match(r'^# Linux/\S* (?P<version>\S*) Kernel Configuration$', sLine)
                  if not match:
                     # Match: “Linux kernel version: 2.6.34”.
                     match = re.match(r'^# Linux kernel version: (?P<version>\S+)', sLine)
                  if match:
                     bConfigVersionFound = match.group('version') == self._m_sKernelVersion
                     continue
               else:
                  self.eerror('This kernel needs to be configured first. Try:')
                  self.eerror('  make -C \'{}\' menuconfig'.format(self._m_sSourcePath))
                  raise GeneratorError()
            elif not sLine.startswith('#'):
               match = re.match(r'^(?P<name>CONFIG_\S+)+=(?P<value>.*)$', sLine)
               if match:
                  sValue = match.group('value')
                  if sValue == 'y':
                     oValue = True
                  elif sValue == 'n' or sValue == 'm':
                     # Consider modules as missing, since checks for CONFIG_* values in this class
                     # would hardly consider modules as satisfying.
                     continue
                  elif len(sValue) >= 2 and sValue.startswith('"') and sValue.endswith('"'):
                     oValue = sValue[1:-1]
                  else:
                     oValue = sValue
                  dictKernelConfig[match.group('name')] = oValue
      self._m_dictKernelConfig = dictKernelConfig

   def make_package_name(self):
      """Generates category, name and version for the binary package that will be generated."""

      self._m_sCategory = 'sys-kernel'
      match = re.match(
         r'(?P<ver>(?:\d+\.)*\d+)-?(?P<extra>.*?)?(?P<rev>(?:-r|_p)\d+)?$', self._m_sKernelVersion
      )
      # Build the package name.
      if match.group('extra'):
         self._m_sPackageName = match.group('extra')
      else:
         self._m_sPackageName = 'vanilla'
      sLocalVersion = self._m_dictKernelConfig.get('CONFIG_LOCALVERSION')
      if sLocalVersion:
         self._m_sPackageName += sLocalVersion
      self._m_sPackageName += '-bin'
      # Build the package name with version.
      self._m_sPackageVersion = match.group('ver')
      if match.group('rev'):
         self._m_sPackageVersion += match.group('rev')

   def package(self, bIrfDebug = False):
      """Generates a Portage binary package (.tbz2) containing the kernel image, in-tree modules,
      and optional initramfs.

      bool bIrfDebug
         If True, the contents of the generated initramfs will be dumped to a file for later
         inspection.
      """

      # Inject the package contents into ${D}.

      self.einfo('Adding kernel image')
      os.mkdir(os.path.join(self._m_sEbuildPkgRoot, 'boot'))
      shutil.copy2(
         self._m_sSrcConfigPath,
         os.path.join(self._m_sEbuildPkgRoot, 'boot/config-' + self._m_sKernelRelease)
      )
      shutil.copy2(
         os.path.join(self._m_sSourcePath, 'System.map'),
         os.path.join(self._m_sEbuildPkgRoot, 'boot/System.map-' + self._m_sKernelRelease)
      )
      shutil.copy2(
         self._m_sSrcImagePath,
         os.path.join(self._m_sEbuildPkgRoot, 'boot/linux-' + self._m_sKernelRelease)
      )
      # Create a symlink for compatibility with GRUB’s /etc/grub.d/10_linux detection script.
      os.symlink(
         'linux-' + self._m_sKernelRelease,
         os.path.join(self._m_sEbuildPkgRoot, 'boot/kernel-' + self._m_sKernelRelease)
      )

      self.einfo('Adding modules')
      self.kmake_check_call('INSTALL_MOD_PATH=' + self._m_sEbuildPkgRoot, 'modules_install')

      if self._m_sIrfSourcePath:
         self._m_sIrfArchivePath = os.path.join(
            self._m_sEbuildPkgRoot, 'boot/initramfs-{}.cpio'.format(self._m_sKernelRelease)
         )
         if self._m_comprIrf:
            self._m_sIrfArchivePath += self._m_comprIrf.file_name_ext()
         self.build_initramfs(bIrfDebug)
         # Create a symlink for compatibility with GRUB’s /etc/grub.d/10_linux detection script.
         os.symlink(
            os.path.basename(self._m_sIrfArchivePath),
            os.path.dirname(self._m_sIrfArchivePath) +
               '/initramfs-{}.img'.format(self._m_sKernelRelease)
         )

      # Complete the package creation, which will grab everything that’s in ${D}.
      self.einfo('Creating package')
      subprocess.check_call(
         ('ebuild', self._m_sEbuildFilePath, 'package'),
         stdout = self._m_fileNullOut, stderr = subprocess.STDOUT
      )

   def prepare(self):
      """Prepares for the execution of the build_kernel() and build_initramfs() methods."""

      self.einfo('Preparing to build kernel')

      # Ensure we have a valid kernel source directory, and get its version.
      if self._m_sSourcePath:
         sKernelVersion = self.kmake_call_kernelversion()
         if not sKernelVersion:
            self.eerror('The path `{}\' doesn\'t seem to be a kernel source directory.'.format(
               self._m_sSourcePath
            ))
            raise GeneratorError()
      else:
         self._m_sSourcePath = os.getcwd()
         sKernelVersion = self.kmake_call_kernelversion()
         if not sKernelVersion:
            # No kernel was found ${PWD}: checking if ony can be found at /usr/src/linux.
            self._m_sSourcePath = os.path.join(self._m_sRoot, 'usr/src/linux')
            if not os.path.isdir(self._m_sSourcePath):
               self.eerror(
                  'No suitable kernel source directory could be found; please specify one using'
               )
               self.eerror('the --source option, or invoke kernel-gen from within a kernel source')
               self.eerror('directory.')
               self.eerror(
                  'Alternatively, you can enable the \033[1;34msymlink\033[0m USE flag to keep ' +
                  'an up-to-date'
               )
               self.eerror(
                  'symlink to your current kernel source directory in ' +
                  '\033[1;36m/usr/src/linux\033[0m.'
               )
               raise GeneratorError()
            sKernelVersion = self.kmake_call_kernelversion()
            if not sKernelVersion:
               self.eerror('Unable to determine the version of the selected kernel source.')
               raise GeneratorError()
      # self._m_sSourcePath is valid; make it permanently part of self._m_listKMakeArgs.
      self._m_listKMakeArgs[1:1] = ['--directory', self._m_sSourcePath]
      self._m_sKernelVersion = sKernelVersion

      self._m_sSourcePath = os.path.abspath(self._m_sSourcePath)
      self._m_sSrcConfigPath = os.path.join(self._m_sSourcePath, '.config')

      # Verify that the kernel has been configured, and get its release string (= version + local).
      self.load_kernel_config()
      self._m_sKernelRelease = self.kmake_check_output('kernelrelease')

      # Get compressor to use for the kernel image from the config file.
      for compr in self._smc_listCompressors:
         if ('CONFIG_KERNEL_' + compr.config_name()) in self._m_dictKernelConfig:
            comprKernel = compr
            break
      else:
         comprKernel = None

      # Determine the location of the generated kernel image.
      sImagePath = self.kmake_check_output('image_name')
      self._m_sSrcImagePath = os.path.join(self._m_sSourcePath, sImagePath)
      del sImagePath

      if self._m_sIrfSourcePath:
         # Check for initramfs/initrd support with the config file.
         if 'CONFIG_BLK_DEV_INITRD' not in self._m_dictKernelConfig:
            self.eerror('The selected kernel was not configured to support initramfs/initrd.')
            raise GeneratorError()
         if self._m_sIrfSourcePath is True:
            self._m_sIrfSourcePath = os.path.join(self._m_sRoot, 'usr/src/initramfs')
            if not os.path.isdir(self._m_sIrfSourcePath):
               self.ewarn('The selected kernel was configured to support initramfs/initrd,')
               self.ewarn('but no suitable initramfs source directory was specified or found.')
               self.ewarn('No initramfs will be created.')
               self._m_sIrfSourcePath = False
         else:
            if not os.path.isdir(self._m_sIrfSourcePath):
               self.eerror('The initramfs path `{}\' is not a directory.'.format(
                  self._m_sIrfSourcePath
               ))
               raise GeneratorError()

      if self._m_sIrfSourcePath:
         # TODO: check that these CONFIG_ match:
         #   +DEVTMPFS

         # Check for an enabled initramfs compression method.
         listEnabledIrfCompressors = []
         for compr in self._smc_listCompressors:
            if ('CONFIG_RD_' + compr.config_name()) in self._m_dictKernelConfig:
               if compr is comprKernel:
                  # We can pick the same compression for kernel image and initramfs.
                  self._m_comprIrf = comprKernel
                  break
               # Not the same as the kernel image, but make a note of this in case the condition
               # above is never satisfied.
               listEnabledIrfCompressors.append(compr)
         else:
            if listEnabledIrfCompressors:
               # Pick the first enabled compression method, if any.
               self._m_comprIrf = listEnabledIrfCompressors[0]

      # Determine if cross-compiling.
      self._m_sCrossCompiler = self._m_dictKernelConfig.get('CONFIG_CROSS_COMPILE')
