# -*- coding: utf-8; mode: python; tab-width: 3; indent-tabs-mode: nil -*-
#
# Copyright 2013, 2017 Raffaello D. Di Napoli
#
# This file is part of kernel-tools.
#
# kernel-tools is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# kernel-tools is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# kernel-tools. If not, see <http://www.gnu.org/licenses/>.
#-----------------------------------------------------------------------------

"""Tools to build and maintain Linux from sources."""

from .OutOfTreeEnumerator import OutOfTreeEnumerator
from .Generator import Generator, GeneratorError
