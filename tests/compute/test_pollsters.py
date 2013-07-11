# -*- encoding: utf-8 -*-
#
# Copyright © 2012 eNovance <licensing@enovance.com>
# Copyright © 2012 Red Hat, Inc
#
# Author: Julien Danjou <julien@danjou.info>
# Author: Eoghan Glynn <eglynn@redhat.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Tests for the compute pollsters.
"""

import mock
import time

from ceilometer.compute import manager
from ceilometer.compute import pollsters
from ceilometer.compute.virt import inspector as virt_inspector
from ceilometer.tests import base as test_base


class FauxInstance(object):

    def __init__(self, **kwds):
        for name, value in kwds.items():
            setattr(self, name, value)

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default):
        try:
            return getattr(self, key)
        except AttributeError:
            return default


class TestLocationMetadata(test_base.TestCase):

    @mock.patch('ceilometer.pipeline.setup_pipeline', mock.MagicMock())
    def setUp(self):
        self.manager = manager.AgentManager()
        super(TestLocationMetadata, self).setUp()

        # Mimics an instance returned from nova api call
        self.INSTANCE_PROPERTIES = {'name': 'display name',
                                    'OS-EXT-SRV-ATTR:instance_name':
                                    'instance-000001',
                                    'OS-EXT-AZ:availability_zone':
                                    'foo-zone',
                                    'reservation_id': 'reservation id',
                                    'architecture': 'x86_64',
                                    'kernel_id': 'kernel id',
                                    'os_type': 'linux',
                                    'ramdisk_id': 'ramdisk id',
                                    'ephemeral_gb': 7,
                                    'root_gb': 3,
                                    'image': {'id': 1,
                                              'links': [{"rel": "bookmark",
                                                         'href': 2}]},
                                    'hostId': '1234-5678',
                                    'flavor': {'id': 1,
                                               'disk': 0,
                                               'ram': 512,
                                               'vcpus': 2},
                                    'metadata': {'metering.autoscale.group':
                                                 'X' * 512,
                                                 'metering.ephemeral_gb': 42}}

        self.instance = FauxInstance(**self.INSTANCE_PROPERTIES)

    def test_metadata(self):
        md = pollsters._get_metadata_from_object(self.instance)
        for prop, value in self.INSTANCE_PROPERTIES.iteritems():
            if prop not in ("metadata"):
                # Special cases
                if prop == 'name':
                    prop = 'display_name'
                elif prop == 'hostId':
                    prop = "host"
                elif prop == 'OS-EXT-SRV-ATTR:instance_name':
                    prop = 'name'
                self.assertEqual(md[prop], value)
        user_metadata = md['user_metadata']
        expected = self.INSTANCE_PROPERTIES[
            'metadata']['metering.autoscale.group'][:256]
        self.assertEqual(user_metadata['autoscale_group'], expected)
        self.assertEqual(len(user_metadata), 1)

    def test_metadata_empty_image(self):
        self.INSTANCE_PROPERTIES['image'] = ''
        self.instance = FauxInstance(**self.INSTANCE_PROPERTIES)
        md = pollsters._get_metadata_from_object(self.instance)
        self.assertEqual(md['image_ref'], None)
        self.assertEqual(md['image_ref_url'], None)


class TestPollsterBase(test_base.TestCase):

    def setUp(self):
        super(TestPollsterBase, self).setUp()
        self.mox.StubOutWithMock(virt_inspector, 'get_hypervisor_inspector')
        self.inspector = self.mox.CreateMock(virt_inspector.Inspector)
        virt_inspector.get_hypervisor_inspector().AndReturn(self.inspector)
        self.instance = mock.MagicMock()
        self.instance.name = 'instance-00000001'
        setattr(self.instance, 'OS-EXT-SRV-ATTR:instance_name',
                self.instance.name)
        self.instance.id = 1
        self.instance.flavor = {'name': 'm1.small', 'id': 2, 'vcpus': 1,
                                'ram': 512, 'disk': 0}


class TestInstancePollster(TestPollsterBase):

    def setUp(self):
        super(TestInstancePollster, self).setUp()

    @mock.patch('ceilometer.pipeline.setup_pipeline', mock.MagicMock())
    def test_get_counters(self):
        self.mox.ReplayAll()

        mgr = manager.AgentManager()
        pollster = pollsters.InstancePollster()
        counters = list(pollster.get_counters(mgr, {}, self.instance))
        self.assertEquals(len(counters), 2)
        self.assertEqual(counters[0].name, 'instance')
        self.assertEqual(counters[1].name, 'instance:m1.small')


class TestDiskIOPollster(TestPollsterBase):

    def setUp(self):
        super(TestDiskIOPollster, self).setUp()

    @mock.patch('ceilometer.pipeline.setup_pipeline', mock.MagicMock())
    def test_get_counters(self):
        disks = [
            (virt_inspector.Disk(device='vda'),
             virt_inspector.DiskStats(read_bytes=1L, read_requests=2L,
                                      write_bytes=3L, write_requests=4L,
                                      errors=-1L))
        ]
        self.inspector.inspect_disks(self.instance.name).AndReturn(disks)
        self.mox.ReplayAll()

        mgr = manager.AgentManager()
        pollster = pollsters.DiskIOPollster()
        cache = {}
        counters = list(pollster.get_counters(mgr, cache, self.instance))
        assert counters
        assert pollster.CACHE_KEY_DISK in cache
        assert self.instance.name in cache[pollster.CACHE_KEY_DISK]

        self.assertEqual(set([c.name for c in counters]),
                         set(pollster.get_counter_names()))

        def _verify_disk_metering(name, expected_volume):
            match = [c for c in counters if c.name == name]
            self.assertEquals(len(match), 1, 'missing counter %s' % name)
            self.assertEquals(match[0].volume, expected_volume)
            self.assertEquals(match[0].type, 'cumulative')

        _verify_disk_metering('disk.read.requests', 2L)
        _verify_disk_metering('disk.read.bytes', 1L)
        _verify_disk_metering('disk.write.requests', 4L)
        _verify_disk_metering('disk.write.bytes', 3L)

    @mock.patch('ceilometer.pipeline.setup_pipeline', mock.MagicMock())
    def test_get_counters_cache(self):
        self.mox.ReplayAll()

        mgr = manager.AgentManager()
        pollster = pollsters.DiskIOPollster()
        cache = {
            pollster.CACHE_KEY_DISK: {
                self.instance.name: pollsters.DiskIOData(
                    r_bytes=-1,
                    r_requests=-2,
                    w_bytes=-3,
                    w_requests=-4,
                ),
            },
        }

        counters = list(pollster.get_counters(mgr, cache, self.instance))
        assert counters

        self.assertEqual(set([c.name for c in counters]),
                         set(pollster.get_counter_names()))

        def _verify_disk_metering(name, expected_volume):
            match = [c for c in counters if c.name == name]
            self.assertEquals(len(match), 1, 'missing counter %s' % name)
            self.assertEquals(match[0].volume, expected_volume)
            self.assertEquals(match[0].type, 'cumulative')

        _verify_disk_metering('disk.read.requests', -2L)
        _verify_disk_metering('disk.read.bytes', -1L)
        _verify_disk_metering('disk.write.requests', -4L)
        _verify_disk_metering('disk.write.bytes', -3L)


class TestNetPollster(TestPollsterBase):

    def setUp(self):
        super(TestNetPollster, self).setUp()

    @mock.patch('ceilometer.pipeline.setup_pipeline', mock.MagicMock())
    def test_get_counters(self):
        vnic0 = virt_inspector.Interface(
            name='vnet0',
            fref='fa163e71ec6e',
            mac='fa:16:3e:71:ec:6d',
            parameters=dict(ip='10.0.0.2',
                            projmask='255.255.255.0',
                            projnet='proj1',
                            dhcp_server='10.0.0.1'))
        stats0 = virt_inspector.InterfaceStats(rx_bytes=1L, rx_packets=2L,
                                               tx_bytes=3L, tx_packets=4L)
        vnic1 = virt_inspector.Interface(
            name='vnet1',
            fref='fa163e71ec6f',
            mac='fa:16:3e:71:ec:6e',
            parameters=dict(ip='192.168.0.3',
                            projmask='255.255.255.0',
                            projnet='proj2',
                            dhcp_server='10.0.0.2'))
        stats1 = virt_inspector.InterfaceStats(rx_bytes=5L, rx_packets=6L,
                                               tx_bytes=7L, tx_packets=8L)
        vnic2 = virt_inspector.Interface(
            name='vnet2',
            fref=None,
            mac='fa:18:4e:72:fc:7e',
            parameters=dict(ip='192.168.0.4',
                            projmask='255.255.255.0',
                            projnet='proj3',
                            dhcp_server='10.0.0.3'))
        stats2 = virt_inspector.InterfaceStats(rx_bytes=9L, rx_packets=10L,
                                               tx_bytes=11L, tx_packets=12L)

        vnics = [(vnic0, stats0), (vnic1, stats1), (vnic2, stats2)]

        self.inspector.inspect_vnics(self.instance.name).AndReturn(vnics)
        self.mox.ReplayAll()

        mgr = manager.AgentManager()
        pollster = pollsters.NetPollster()
        counters = list(pollster.get_counters(mgr, {}, self.instance))
        assert counters
        self.assertEqual(set([c.name for c in counters]),
                         set(pollster.get_counter_names()))

        def _verify_vnic_metering(name, ip, expected_volume, expected_rid):
            match = [c for c in counters if c.name == name and
                     c.resource_metadata['parameters']['ip'] == ip]
            self.assertEquals(len(match), 1, 'missing counter %s' % name)
            self.assertEquals(match[0].volume, expected_volume)
            self.assertEquals(match[0].type, 'cumulative')
            self.assertEquals(match[0].resource_id, expected_rid)

        instance_name_id = "%s-%s" % (self.instance.name, self.instance.id)
        _verify_vnic_metering('network.incoming.bytes', '10.0.0.2', 1L,
                              vnic0.fref)
        _verify_vnic_metering('network.incoming.bytes', '192.168.0.3', 5L,
                              vnic1.fref)
        _verify_vnic_metering('network.incoming.bytes', '192.168.0.4', 9L,
                              "%s-%s" % (instance_name_id, vnic2.name))
        _verify_vnic_metering('network.outgoing.bytes', '10.0.0.2', 3L,
                              vnic0.fref)
        _verify_vnic_metering('network.outgoing.bytes', '192.168.0.3', 7L,
                              vnic1.fref)
        _verify_vnic_metering('network.outgoing.bytes', '192.168.0.4', 11L,
                              "%s-%s" % (instance_name_id, vnic2.name))
        _verify_vnic_metering('network.incoming.packets', '10.0.0.2', 2L,
                              vnic0.fref)
        _verify_vnic_metering('network.incoming.packets', '192.168.0.3', 6L,
                              vnic1.fref)
        _verify_vnic_metering('network.incoming.packets', '192.168.0.4', 10L,
                              "%s-%s" % (instance_name_id, vnic2.name))
        _verify_vnic_metering('network.outgoing.packets', '10.0.0.2', 4L,
                              vnic0.fref)
        _verify_vnic_metering('network.outgoing.packets', '192.168.0.3', 8L,
                              vnic1.fref)
        _verify_vnic_metering('network.outgoing.packets', '192.168.0.4', 12L,
                              "%s-%s" % (instance_name_id, vnic2.name))


class TestCPUPollster(TestPollsterBase):

    def setUp(self):
        super(TestCPUPollster, self).setUp()

    @mock.patch('ceilometer.pipeline.setup_pipeline', mock.MagicMock())
    def test_get_counters(self):
        self.inspector.inspect_cpus(self.instance.name).AndReturn(
            virt_inspector.CPUStats(time=1 * (10 ** 6), number=2))
        self.inspector.inspect_cpus(self.instance.name).AndReturn(
            virt_inspector.CPUStats(time=3 * (10 ** 6), number=2))
        # cpu_time resets on instance restart
        self.inspector.inspect_cpus(self.instance.name).AndReturn(
            virt_inspector.CPUStats(time=2 * (10 ** 6), number=2))
        self.mox.ReplayAll()

        mgr = manager.AgentManager()
        pollster = pollsters.CPUPollster()

        def _verify_cpu_metering(zero, expected_time):
            cache = {}
            counters = list(pollster.get_counters(mgr, cache, self.instance))
            self.assertEquals(len(counters), 2)
            self.assertEqual(set([c.name for c in counters]),
                             set(pollster.get_counter_names()))
            assert counters[0].name == 'cpu_util'
            assert (counters[0].volume == 0.0 if zero else
                    counters[0].volume > 0.0)
            assert counters[1].name == 'cpu'
            assert counters[1].volume == expected_time
            assert pollster.CACHE_KEY_CPU in cache
            assert self.instance.name in cache[pollster.CACHE_KEY_CPU]
            # ensure elapsed time between polling cycles is non-zero
            time.sleep(0.001)

        _verify_cpu_metering(True, 1 * (10 ** 6))
        _verify_cpu_metering(False, 3 * (10 ** 6))
        _verify_cpu_metering(False, 2 * (10 ** 6))

    @mock.patch('ceilometer.pipeline.setup_pipeline', mock.MagicMock())
    def test_get_counters_cache(self):
        # self.inspector.inspect_cpus(self.instance.name).AndReturn(
        #     virt_inspector.CPUStats(time=1 * (10 ** 6), number=2))
        # self.inspector.inspect_cpus(self.instance.name).AndReturn(
        #     virt_inspector.CPUStats(time=3 * (10 ** 6), number=2))
        # # cpu_time resets on instance restart
        # self.inspector.inspect_cpus(self.instance.name).AndReturn(
        #     virt_inspector.CPUStats(time=2 * (10 ** 6), number=2))
        self.mox.ReplayAll()

        mgr = manager.AgentManager()
        pollster = pollsters.CPUPollster()

        cache = {
            pollster.CACHE_KEY_CPU: {
                self.instance.name: virt_inspector.CPUStats(
                    time=10 ** 6,
                    number=2,
                ),
            },
        }
        counters = list(pollster.get_counters(mgr, cache, self.instance))
        self.assertEquals(len(counters), 2)
        self.assertEquals(counters[1].volume, 10 ** 6)
