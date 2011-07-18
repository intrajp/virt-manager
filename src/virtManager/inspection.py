#
# Copyright (C) 2011 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.
#

import time
from Queue import Queue, Empty
from threading import Thread

from guestfs import GuestFS

import logging

class vmmInspection(Thread):
    _name = "inspection thread"
    _wait = 15 # seconds

    def __init__(self):
        Thread.__init__(self, name=self._name)
        self._q = Queue()
        self._conns = dict()
        self._vmseen = dict()

    # Called by the main thread whenever a connection is added or
    # removed.  We tell the inspection thread, so it can track
    # connections.
    def conn_added(self, engine_ignore, conn):
        obj = ("conn_added", conn)
        self._q.put(obj)

    def conn_removed(self, engine_ignore, uri):
        obj = ("conn_removed", uri)
        self._q.put(obj)

    # Called by the main thread whenever a VM is added to vmlist.
    def vm_added(self):
        obj = ("vm_added")
        self._q.put(obj)

    def run(self):
        # Wait a few seconds before we do anything.  This prevents
        # inspection from being a burden for initial virt-manager
        # interactivity (although it shouldn't affect interactivity at
        # all).
        logging.debug("%s: waiting" % self._name)
        time.sleep(self._wait)

        while True:
            logging.debug("%s: ready" % self._name)
            self._process_queue()
            self._process_vms()

    # Process everything on the queue.  If the queue is empty when
    # called, block.
    def _process_queue(self):
        first_obj = self._q.get()
        self._process_queue_item(first_obj)
        self._q.task_done()
        try:
            while True:
                obj = self._q.get(False)
                self._process_queue_item(obj)
                self._q.task_done()
        except Empty:
            pass

    def _process_queue_item(self, obj):
        if obj[0] == "conn_added":
            conn = obj[1]
            if conn and not (conn.is_remote()):
                uri = conn.get_uri()
                self._conns[uri] = conn
        elif obj[0] == "conn_removed":
            uri = obj[1]
            del self._conns[uri]
        elif obj[0] == "vm_added":
            # Nothing - just a signal for the inspection thread to wake up.
            pass

    # Any VMs we've not seen yet?  If so, process them.
    def _process_vms(self):
        for conn in self._conns.itervalues():
            for vmuuid in conn.list_vm_uuids():
                if not (vmuuid in self._vmseen):
                    logging.debug("%s: processing started on '%s'" %
                                  (self._name, vmuuid))
                    try:
                        vm = conn.get_vm(vmuuid)
                        # Whether success or failure, we've "seen" this VM now.
                        self._vmseen[vmuuid] = True
                        self._process(conn, vm, vmuuid)
                    except:
                        logging.exception("%s: exception while processing '%s'" %
                                          (self._name, vmuuid))
                    logging.debug("%s: processing done on '%s'" %
                                  (self._name, vmuuid))

    def _process(self, conn_ignore, vm, vmuuid):
        # Add the disks.  Note they *must* be added with readonly flag set.
        g = GuestFS()
        for disk in vm.get_disk_devices():
            path = disk.path
            driver_type = disk.driver_type
            if (disk.type == "block" or disk.type == "file") and path != None:
                g.add_drive_opts(path, readonly=1, format=driver_type)

        g.launch()

        # Inspect the operating system.
        roots = g.inspect_os()
        if len(roots) == 0:
            logging.debug("%s: %s: no operating systems found" %
                          (self._name, vmuuid))
            return

        # Arbitrarily pick the first root device.
        root = roots[0]

        # Inspection results.
        typ = g.inspect_get_type(root) # eg. "linux"
        distro = g.inspect_get_distro(root) # eg. "fedora"
        major_version = g.inspect_get_major_version(root) # eg. 14
        minor_version = g.inspect_get_minor_version(root) # eg. 0
        hostname = g.inspect_get_hostname(root) # string
        product_name = g.inspect_get_product_name(root) # string

        # Added in libguestfs 1.9.13:
        product_variant = None
        if hasattr(g, "inspect_get_product_variant"):
            product_variant = g.inspect_get_product_variant(root) # string

        # For inspect_list_applications and inspect_get_icon we
        # require that the guest filesystems are mounted.  However
        # don't fail if this is not possible (I'm looking at you,
        # FreeBSD).
        filesystems_mounted = False
        try:
            # Mount up the disks, like guestfish --ro -i.

            # Sort keys by length, shortest first, so that we end up
            # mounting the filesystems in the correct order.
            mps = g.inspect_get_mountpoints(root)
            def compare(a, b):
                if len(a[0]) > len(b[0]):
                    return 1
                elif len(a[0]) == len(b[0]):
                    return 0
                else:
                    return -1
            mps.sort(compare)

            for mp_dev in mps:
                try:
                    g.mount_ro(mp_dev[1], mp_dev[0])
                except:
                    logging.exception("%s: exception mounting %s on %s "
                                      "(ignored)" % (
                            self._name,
                            mp_dev[1], mp_dev[0]))

            filesystems_mounted = True
        except:
            logging.exception("%s: exception while mounting disks (ignored)" %
                              self._name)

        icon = None
        apps = None
        if filesystems_mounted:
            # Added in libguestfs 1.11.12:
            if hasattr(g, "inspect_get_icon"):
                # string containing PNG data
                icon = g.inspect_get_icon(root, favicon=0, highquality=1)
                if icon == "":
                    icon = None

            # Inspection applications.
            apps = g.inspect_list_applications(root)

        # Force the libguestfs handle to close right now.
        del g

        # Log what we found.
        logging.debug("%s: detected operating system: %s %s %d.%d (%s)",
                      self._name, typ, distro, major_version, minor_version,
                      product_name)
        logging.debug("%s: hostname: %s", self._name, hostname)
        if icon:
            logging.debug("%s: icon: %d bytes", self._name, len(icon))
        if apps:
            logging.debug("%s: # apps: %d", self._name, len(apps))
