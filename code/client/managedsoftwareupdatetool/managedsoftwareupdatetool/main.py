# -*- coding: utf-8 -*-
#
#  main.py
#  managedsoftwareupdatetool
#
#  Created by Greg Neagle on 8/5/15.
#  Copyright (c) 2015 The Munki Project. All rights reserved.
#

# import modules required by application
import objc
import Foundation
import AppKit

from PyObjCTools import AppHelper

# import modules containing classes required to start application and load MainMenu.nib
import AppDelegate

# pass control to AppKit
AppHelper.runEventLoop(installInterrupt=True)
