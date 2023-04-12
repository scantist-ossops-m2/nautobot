from unittest import skipIf

import netaddr
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase

from nautobot.core.testing.models import ModelTestCases
from nautobot.dcim import choices as dcim_choices
from nautobot.dcim.models import Device, DeviceType, Interface, Location, LocationType
from nautobot.extras.models import Role, Status
from nautobot.ipam.choices import IPAddressStatusChoices, PrefixTypeChoices
from nautobot.ipam.models import IPAddress, Prefix, VLAN, VLANGroup, VRF
from nautobot.virtualization.models import Cluster, ClusterType, VirtualMachine, VMInterface


class IPAddressToInterfaceTest(TestCase):
    """Tests for `nautobot.ipam.models.IPAddressToInterface`."""

    @classmethod
    def setUpTestData(cls):
        cls.test_device = Device.objects.create(
            name="device1",
            role=Role.objects.get_for_model(Device).first(),
            device_type=DeviceType.objects.first(),
            location=Location.objects.get_for_model(Device).first(),
            status=Status.objects.get_for_model(Device).first(),
        )
        int_status = Status.objects.get_for_model(Interface).first()
        cls.test_int1 = Interface.objects.create(
            device=cls.test_device,
            name="int1",
            status=int_status,
            type=dcim_choices.InterfaceTypeChoices.TYPE_1GE_FIXED,
        )
        cls.test_int2 = Interface.objects.create(
            device=cls.test_device,
            name="int2",
            status=int_status,
            type=dcim_choices.InterfaceTypeChoices.TYPE_1GE_FIXED,
        )
        cluster_type = ClusterType.objects.create(name="Cluster Type 1")
        cluster = Cluster.objects.create(name="cluster1", cluster_type=cluster_type)
        vmint_status = Status.objects.get_for_model(VMInterface).first()
        cls.test_vm = VirtualMachine.objects.create(
            name="vm1",
            cluster=cluster,
            status=Status.objects.get_for_model(VirtualMachine).first(),
        )
        cls.test_vmint1 = VMInterface.objects.create(
            name="vmint1",
            virtual_machine=cls.test_vm,
            status=vmint_status,
        )
        cls.test_vmint2 = VMInterface.objects.create(
            name="vmint2",
            virtual_machine=cls.test_vm,
            status=vmint_status,
        )


class TestVarbinaryIPField(TestCase):
    """Tests for `nautobot.ipam.fields.VarbinaryIPField`."""

    def setUp(self):
        super().setUp()

        # Field is a VarbinaryIPField we'll use to test.
        self.prefix = Prefix.objects.create(prefix="10.0.0.0/24")
        self.field = self.prefix._meta.get_field("network")
        self.network = self.prefix.network
        self.network_packed = bytes(self.prefix.prefix.network)

    def test_db_type(self):
        """Test `VarbinaryIPField.db_type`."""
        # Mapping of vendor -> db_type
        db_types = {
            "postgresql": "bytea",
            "mysql": "varbinary(16)",
        }

        expected = db_types[connection.vendor]
        self.assertEqual(self.field.db_type(connection), expected)

    def test_value_to_string(self):
        """Test `VarbinaryIPField.value_to_string`."""
        # value_to_string calls _parse_address so no need for negative tests here.
        self.assertEqual(self.field.value_to_string(self.prefix), self.network)

    def test_parse_address_success(self):
        """Test `VarbinaryIPField._parse_address` PASS."""

        # str => netaddr.IPAddress
        obj = self.field._parse_address(self.prefix.network)
        self.assertEqual(obj, netaddr.IPAddress(self.network))

        # bytes => netaddr.IPAddress
        self.assertEqual(self.field._parse_address(bytes(obj)), obj)

        # int => netaddr.IPAddress
        self.assertEqual(self.field._parse_address(int(obj)), obj)

        # IPAddress => netaddr.IPAddress
        self.assertEqual(self.field._parse_address(obj), obj)

        # Special cases involving values that could be IPv4 or IPv6 if naively interpreted
        self.assertEqual(self.field._parse_address(bytes(netaddr.IPAddress("0.0.0.1"))), netaddr.IPAddress("0.0.0.1"))
        self.assertEqual(self.field._parse_address(bytes(netaddr.IPAddress("::1"))), netaddr.IPAddress("::1"))
        self.assertEqual(
            self.field._parse_address(bytes(netaddr.IPAddress("::192.0.2.15"))), netaddr.IPAddress("::192.0.2.15")
        )

    def test_parse_address_failure(self):
        """Test `VarbinaryIPField._parse_address` FAIL."""

        bad_inputs = (
            None,
            -42,
            "10.10.10.10/32",  # Prefixes not allowed here
            "310.10.10.10",  # Bad IP
        )
        for bad in bad_inputs:
            self.assertRaises(ValidationError, self.field._parse_address, bad)

    def test_to_python(self):
        """Test `VarbinaryIPField.to_python`."""

        # to_python calls _parse_address so no need for negative tests here.

        # str => str
        self.assertEqual(self.field.to_python(self.prefix.network), self.network)

        # netaddr.IPAddress => str
        self.assertEqual(self.field.to_python(self.prefix.prefix.ip), self.network)

    @skipIf(
        connection.vendor != "postgresql",
        "postgres is not the database driver",
    )
    def test_get_db_prep_value_postgres(self):
        """Test `VarbinaryIPField.get_db_prep_value`."""

        # PostgreSQL escapes `bytes` in `::bytea` and you must call
        # `getquoted()` to extract the value.
        prepped = self.field.get_db_prep_value(self.network, connection)
        manual = connection.Database.Binary(self.network_packed)
        self.assertEqual(prepped.getquoted(), manual.getquoted())

    @skipIf(
        connection.vendor != "mysql",
        "mysql is not the database driver",
    )
    def test_get_db_prep_value_mysql(self):
        """Test `VarbinaryIPField.get_db_prep_value` for MySQL."""

        # MySQL uses raw `bytes`
        prepped = self.field.get_db_prep_value(self.network, connection)
        manual = bytes(self.network_packed)
        self.assertEqual(prepped, manual)


class TestPrefix(ModelTestCases.BaseModelTestCase):
    model = Prefix

    def setUp(self):
        super().setUp()
        # With advent of `Prefix.parent`, Prefixes can't just be bulk deleted without clearing their
        # `parent` first in an `update()` query which doesn't call `save()` or `fire `(pre|post)_save` signals.
        Prefix.objects.update(parent=None)
        Prefix.objects.all().delete()
        IPAddress.objects.all().delete()
        self.statuses = Status.objects.get_for_model(Prefix)
        self.status = self.statuses.first()
        self.root = Prefix.objects.create(prefix="101.102.0.0/24", status=self.status)
        self.parent = Prefix.objects.create(prefix="101.102.0.0/25", status=self.status)
        self.child1 = Prefix.objects.create(prefix="101.102.0.0/26", status=self.status)
        self.child2 = Prefix.objects.create(prefix="101.102.0.64/26", status=self.status)

    def test_prefix_validation(self):
        location_type = LocationType.objects.get(name="Room")
        location = Location.objects.filter(location_type=location_type).first()
        prefix = Prefix(prefix=netaddr.IPNetwork("192.0.2.0/24"), location=location)
        prefix.status = self.statuses[0]
        with self.assertRaises(ValidationError) as cm:
            prefix.validated_save()
        self.assertIn(f'Prefixes may not associate to locations of type "{location_type.name}"', str(cm.exception))

    def test_tree_methods(self):
        """Test the various tree methods work as expected."""

        # supernets()
        self.assertEqual(list(self.root.supernets()), [])
        self.assertEqual(list(self.child1.supernets()), [self.root, self.parent])
        self.assertEqual(list(self.child1.supernets(include_self=True)), [self.root, self.parent, self.child1])
        self.assertEqual(list(self.child1.supernets(direct=True)), [self.parent])

        # subnets()
        self.assertEqual(list(self.root.subnets()), [self.parent, self.child1, self.child2])
        self.assertEqual(list(self.root.subnets(direct=True)), [self.parent])
        self.assertEqual(list(self.root.subnets(include_self=True)), [self.root, self.parent, self.child1, self.child2])

        # is_child_node()
        self.assertFalse(self.root.is_child_node())
        self.assertTrue(self.parent.is_child_node())
        self.assertTrue(self.child1.is_child_node())

        # is_leaf_node()
        self.assertFalse(self.root.is_leaf_node())
        self.assertFalse(self.parent.is_leaf_node())
        self.assertTrue(self.child1.is_leaf_node())

        # is_root_node()
        self.assertTrue(self.root.is_root_node())
        self.assertFalse(self.parent.is_leaf_node())
        self.assertFalse(self.child1.is_root_node())

        # ancestors()
        self.assertEqual(list(self.child1.ancestors()), [self.root, self.parent])
        self.assertEqual(list(self.child1.ancestors(ascending=True)), [self.parent, self.root])
        self.assertEqual(list(self.child1.ancestors(include_self=True)), [self.root, self.parent, self.child1])

        # children.all()
        self.assertEqual(list(self.parent.children.all()), [self.child1, self.child2])

        # descendants()
        self.assertEqual(list(self.root.descendants()), [self.parent, self.child1, self.child2])
        self.assertEqual(
            list(self.root.descendants(include_self=True)), [self.root, self.parent, self.child1, self.child2]
        )

        # root()
        self.assertEqual(self.child1.root(), self.root)
        self.assertIsNone(self.root.root())

        # siblings()
        self.assertEqual(list(self.child1.siblings()), [self.child2])
        self.assertEqual(list(self.child1.siblings(include_self=True)), [self.child1, self.child2])
        parent2 = Prefix.objects.create(prefix="101.102.0.128/25", status=self.status)
        self.assertEqual(list(self.parent.siblings()), [parent2])
        self.assertEqual(list(self.parent.siblings(include_self=True)), [self.parent, parent2])

    # TODO(jathan): When Namespaces are implemented, these tests must be extended to assert it.
    def test_reparenting(self):
        """Test that reparenting algorithm works in its most basic form."""
        # tree hierarchy
        self.assertIsNone(self.root.parent)
        self.assertEqual(self.parent.parent, self.root)
        self.assertEqual(self.child1.parent, self.parent)

        # Delete the parent (/25); child1/child2 now have root (/24) as their parent.
        num_deleted, _ = self.parent.delete()
        self.assertEqual(num_deleted, 1)

        self.assertEqual(list(self.root.children.all()), [self.child1, self.child2])
        self.child1.refresh_from_db()
        self.child2.refresh_from_db()
        self.assertEqual(self.child1.parent, self.root)
        self.assertEqual(self.child2.parent, self.root)
        self.assertEqual(list(self.child1.ancestors()), [self.root])

        # Add /25 back in as a partn and assert that child1/child2 now have it as their parent, and
        # /24 is its parent.
        self.parent.save()  # This creates another Prefix using the same instance.
        self.child1.refresh_from_db()
        self.child2.refresh_from_db()
        self.assertEqual(self.child1.parent, self.parent)
        self.assertEqual(self.child2.parent, self.parent)
        self.assertEqual(list(self.child1.ancestors()), [self.root, self.parent])

    def test_descendants(self):
        vrfs = VRF.objects.all()[:3]
        prefixes = (
            Prefix.objects.create(prefix=netaddr.IPNetwork("10.0.0.0/16"), type=PrefixTypeChoices.TYPE_CONTAINER),
            Prefix.objects.create(prefix=netaddr.IPNetwork("10.0.0.0/24"), vrf=None),
            Prefix.objects.create(prefix=netaddr.IPNetwork("10.0.1.0/24"), vrf=vrfs[0]),
            Prefix.objects.create(prefix=netaddr.IPNetwork("10.0.2.0/24"), vrf=vrfs[1]),
            Prefix.objects.create(prefix=netaddr.IPNetwork("10.0.3.0/24"), vrf=vrfs[2]),
        )
        prefix_pks = {p.pk for p in prefixes[1:]}
        child_prefix_pks = {p.pk for p in prefixes[0].descendants()}

        # Global container should return all children
        self.assertSetEqual(child_prefix_pks, prefix_pks)

        # TODO(jathan): VRF is no longer considered for uniqueness/parenting algorithm, so this
        # check is no longer valid so we'll filter it by VRF for now to keep the test working.
        prefixes[0].vrf = vrfs[0]
        prefixes[0].save()
        child_prefix_pks = {p.pk for p in prefixes[0].descendants().filter(vrf=vrfs[0])}

        # VRF container is limited to its own VRF
        self.assertSetEqual(child_prefix_pks, {prefixes[2].pk})

    def test_get_child_ips(self):
        vrfs = VRF.objects.all()[:3]
        parent_prefix = Prefix.objects.create(
            prefix=netaddr.IPNetwork("10.0.0.0/16"), type=PrefixTypeChoices.TYPE_CONTAINER
        )
        ips = (
            IPAddress.objects.create(address=netaddr.IPNetwork("10.0.0.1/24"), vrf=None),
            IPAddress.objects.create(address=netaddr.IPNetwork("10.0.1.1/24"), vrf=vrfs[0]),
            IPAddress.objects.create(address=netaddr.IPNetwork("10.0.2.1/24"), vrf=vrfs[1]),
            IPAddress.objects.create(address=netaddr.IPNetwork("10.0.3.1/24"), vrf=vrfs[2]),
        )
        child_ip_pks = {p.pk for p in parent_prefix.get_child_ips()}

        # Global container should return all children
        self.assertSetEqual(child_ip_pks, {ips[0].pk, ips[1].pk, ips[2].pk, ips[3].pk})

        parent_prefix.vrf = vrfs[0]
        parent_prefix.save()
        child_ip_pks = {p.pk for p in parent_prefix.get_child_ips()}

        # VRF container is limited to its own VRF
        self.assertSetEqual(child_ip_pks, {ips[1].pk})

        # Make sure /31 is handled correctly
        parent_prefix_31 = Prefix.objects.create(
            prefix=netaddr.IPNetwork("10.0.4.0/31"), type=PrefixTypeChoices.TYPE_CONTAINER
        )
        ips_31 = (
            IPAddress.objects.create(address=netaddr.IPNetwork("10.0.4.0/31"), vrf=None),
            IPAddress.objects.create(address=netaddr.IPNetwork("10.0.4.1/31"), vrf=None),
        )

        child_ip_pks = {p.pk for p in parent_prefix_31.get_child_ips()}
        self.assertSetEqual(child_ip_pks, {ips_31[0].pk, ips_31[1].pk})

    def test_get_available_prefixes(self):
        prefixes = Prefix.objects.bulk_create(
            (
                Prefix(prefix=netaddr.IPNetwork("10.0.0.0/16")),  # Parent prefix
                Prefix(prefix=netaddr.IPNetwork("10.0.0.0/20")),
                Prefix(prefix=netaddr.IPNetwork("10.0.32.0/20")),
                Prefix(prefix=netaddr.IPNetwork("10.0.128.0/18")),
            )
        )
        missing_prefixes = netaddr.IPSet(
            [
                netaddr.IPNetwork("10.0.16.0/20"),
                netaddr.IPNetwork("10.0.48.0/20"),
                netaddr.IPNetwork("10.0.64.0/18"),
                netaddr.IPNetwork("10.0.192.0/18"),
            ]
        )
        available_prefixes = prefixes[0].get_available_prefixes()

        self.assertEqual(available_prefixes, missing_prefixes)

    def test_get_available_ips(self):
        parent_prefix = Prefix.objects.create(prefix=netaddr.IPNetwork("10.0.0.0/28"))
        IPAddress.objects.bulk_create(
            (
                IPAddress(address=netaddr.IPNetwork("10.0.0.1/26")),
                IPAddress(address=netaddr.IPNetwork("10.0.0.3/26")),
                IPAddress(address=netaddr.IPNetwork("10.0.0.5/26")),
                IPAddress(address=netaddr.IPNetwork("10.0.0.7/26")),
                IPAddress(address=netaddr.IPNetwork("10.0.0.9/26")),
                IPAddress(address=netaddr.IPNetwork("10.0.0.11/26")),
                IPAddress(address=netaddr.IPNetwork("10.0.0.13/26")),
            )
        )
        missing_ips = netaddr.IPSet(
            [
                "10.0.0.2/32",
                "10.0.0.4/32",
                "10.0.0.6/32",
                "10.0.0.8/32",
                "10.0.0.10/32",
                "10.0.0.12/32",
                "10.0.0.14/32",
            ]
        )
        available_ips = parent_prefix.get_available_ips()

        self.assertEqual(available_ips, missing_ips)

    def test_get_first_available_prefix(self):
        prefixes = Prefix.objects.bulk_create(
            (
                Prefix(prefix=netaddr.IPNetwork("10.0.0.0/16")),  # Parent prefix
                Prefix(prefix=netaddr.IPNetwork("10.0.0.0/24")),
                Prefix(prefix=netaddr.IPNetwork("10.0.1.0/24")),
                Prefix(prefix=netaddr.IPNetwork("10.0.2.0/24")),
            )
        )
        self.assertEqual(prefixes[0].get_first_available_prefix(), netaddr.IPNetwork("10.0.3.0/24"))

        Prefix.objects.create(prefix=netaddr.IPNetwork("10.0.3.0/24"))
        self.assertEqual(prefixes[0].get_first_available_prefix(), netaddr.IPNetwork("10.0.4.0/22"))

    def test_get_first_available_ip(self):
        parent_prefix = Prefix.objects.create(prefix=netaddr.IPNetwork("10.0.0.0/24"))
        IPAddress.objects.bulk_create(
            (
                IPAddress(address=netaddr.IPNetwork("10.0.0.1/24")),
                IPAddress(address=netaddr.IPNetwork("10.0.0.2/24")),
                IPAddress(address=netaddr.IPNetwork("10.0.0.3/24")),
            )
        )
        self.assertEqual(parent_prefix.get_first_available_ip(), "10.0.0.4/24")

        IPAddress.objects.create(address=netaddr.IPNetwork("10.0.0.4/24"))
        self.assertEqual(parent_prefix.get_first_available_ip(), "10.0.0.5/24")

    def test_get_utilization(self):
        # Container Prefix
        prefix = Prefix.objects.create(prefix=netaddr.IPNetwork("10.0.0.0/24"), type=PrefixTypeChoices.TYPE_CONTAINER)
        Prefix.objects.bulk_create(
            (
                Prefix(prefix=netaddr.IPNetwork("10.0.0.0/26")),
                Prefix(prefix=netaddr.IPNetwork("10.0.0.128/26")),
            )
        )
        self.assertEqual(prefix.get_utilization(), (128, 256))

        # IPv4 Non-container Prefix /24
        prefix.type = PrefixTypeChoices.TYPE_NETWORK
        prefix.save()
        IPAddress.objects.bulk_create(
            # Create 32 IPAddresses within the Prefix
            [IPAddress(address=netaddr.IPNetwork(f"10.0.0.{i}/24")) for i in range(1, 33)]
        )
        # Create IPAddress objects for network and broadcast addresses
        IPAddress.objects.bulk_create(
            (IPAddress(address=netaddr.IPNetwork("10.0.0.0/32")), IPAddress(address=netaddr.IPNetwork("10.0.0.255/32")))
        )
        self.assertEqual(prefix.get_utilization(), (32, 254))

        # Change prefix to a pool, network and broadcast address will count toward numerator and denominator in utilization
        prefix.type = PrefixTypeChoices.TYPE_POOL
        prefix.save()
        self.assertEqual(prefix.get_utilization(), (34, 256))

        # IPv4 Non-container Prefix /31, network and broadcast addresses count toward utilization
        prefix = Prefix.objects.create(prefix="10.0.1.0/31")
        IPAddress.objects.bulk_create(
            (IPAddress(address=netaddr.IPNetwork("10.0.1.0/32")), IPAddress(address=netaddr.IPNetwork("10.0.1.1/32")))
        )
        self.assertEqual(prefix.get_utilization(), (2, 2))

        # IPv6 Non-container Prefix, network and broadcast addresses count toward utilization
        prefix = Prefix.objects.create(prefix="aaaa::/124")
        IPAddress.objects.bulk_create(
            (IPAddress(address=netaddr.IPNetwork("aaaa::0/128")), IPAddress(address=netaddr.IPNetwork("aaaa::f/128")))
        )
        self.assertEqual(prefix.get_utilization(), (2, 16))

        # Large Prefix
        large_prefix = Prefix.objects.create(prefix="22.0.0.0/8", type=PrefixTypeChoices.TYPE_CONTAINER)

        # 25% utilization
        Prefix.objects.bulk_create(
            (
                Prefix(prefix=netaddr.IPNetwork("22.0.0.0/12")),
                Prefix(prefix=netaddr.IPNetwork("22.16.0.0/12")),
                Prefix(prefix=netaddr.IPNetwork("22.32.0.0/12")),
                Prefix(prefix=netaddr.IPNetwork("22.48.0.0/12")),
            )
        )
        self.assertEqual(large_prefix.get_utilization(), (4194304, 16777216))

        # 50% utilization
        Prefix.objects.bulk_create((Prefix(prefix=netaddr.IPNetwork("22.64.0.0/10")),))
        self.assertEqual(large_prefix.get_utilization(), (8388608, 16777216))

        # 100% utilization
        Prefix.objects.bulk_create((Prefix(prefix=netaddr.IPNetwork("22.128.0.0/9")),))
        self.assertEqual(large_prefix.get_utilization(), (16777216, 16777216))

        # IPv6 Large Prefix
        large_prefix_v6 = Prefix.objects.create(prefix="ab00::/8", type=PrefixTypeChoices.TYPE_CONTAINER)

        # 25% utilization
        Prefix.objects.bulk_create(
            (
                Prefix(prefix=netaddr.IPNetwork("ab00::/12")),
                Prefix(prefix=netaddr.IPNetwork("ab10::/12")),
                Prefix(prefix=netaddr.IPNetwork("ab20::/12")),
                Prefix(prefix=netaddr.IPNetwork("ab30::/12")),
            )
        )
        self.assertEqual(large_prefix_v6.get_utilization(), (2**118, 2**120))

        # 50% utilization
        Prefix.objects.bulk_create((Prefix(prefix=netaddr.IPNetwork("ab40::/10")),))
        self.assertEqual(large_prefix_v6.get_utilization(), (2**119, 2**120))

        # 100% utilization
        Prefix.objects.bulk_create((Prefix(prefix=netaddr.IPNetwork("ab80::/9")),))
        self.assertEqual(large_prefix_v6.get_utilization(), (2**120, 2**120))

    #
    # Uniqueness enforcement tests
    #

    def test_duplicate_global_unique(self):
        """This should raise a ValidationError."""
        Prefix.objects.create(prefix=netaddr.IPNetwork("192.0.2.0/24"))
        duplicate_prefix = Prefix(prefix=netaddr.IPNetwork("192.0.2.0/24"))
        self.assertRaises(ValidationError, duplicate_prefix.full_clean)


class TestIPAddress(ModelTestCases.BaseModelTestCase):
    model = IPAddress

    def test_get_duplicates(self):
        ips = (
            IPAddress.objects.create(address=netaddr.IPNetwork("192.0.2.1/24")),
            IPAddress.objects.create(address=netaddr.IPNetwork("192.0.2.1/24")),
            IPAddress.objects.create(address=netaddr.IPNetwork("192.0.2.1/24")),
        )
        duplicate_ip_pks = [p.pk for p in ips[0].get_duplicates()]

        self.assertSetEqual(set(duplicate_ip_pks), {ips[1].pk, ips[2].pk})

    #
    # Uniqueness enforcement tests
    #

    def test_duplicate_global_unique(self):
        IPAddress.objects.create(address=netaddr.IPNetwork("192.0.2.1/24"))
        duplicate_ip = IPAddress(address=netaddr.IPNetwork("192.0.2.1/24"))
        self.assertRaises(ValidationError, duplicate_ip.clean)

    def test_duplicate_nonunique_role(self):
        roles = Role.objects.get_for_model(IPAddress)
        IPAddress.objects.create(
            address=netaddr.IPNetwork("192.0.2.1/24"),
            role=roles[0],
        )
        IPAddress.objects.create(
            address=netaddr.IPNetwork("192.0.2.1/24"),
            role=roles[1],
        )

    def test_multiple_nat_outside_list(self):
        """
        Test suite to test supporing nat_outside_list.
        """
        nat_inside = IPAddress.objects.create(address=netaddr.IPNetwork("192.168.0.1/24"))
        nat_outside1 = IPAddress.objects.create(address=netaddr.IPNetwork("192.0.2.1/24"), nat_inside=nat_inside)
        nat_outside2 = IPAddress.objects.create(address=netaddr.IPNetwork("192.0.2.2/24"), nat_inside=nat_inside)
        nat_outside3 = IPAddress.objects.create(address=netaddr.IPNetwork("192.0.2.3/24"), nat_inside=nat_inside)
        nat_inside.refresh_from_db()
        self.assertEqual(nat_inside.nat_outside_list.count(), 3)
        self.assertEqual(nat_inside.nat_outside_list.all()[0], nat_outside1)
        self.assertEqual(nat_inside.nat_outside_list.all()[1], nat_outside2)
        self.assertEqual(nat_inside.nat_outside_list.all()[2], nat_outside3)

    def test_create_ip_address_without_slaac_status(self):
        slaac_status_name = IPAddressStatusChoices.as_dict()[IPAddressStatusChoices.STATUS_SLAAC]
        IPAddress.objects.filter(status__name=slaac_status_name).delete()
        Status.objects.get(name=slaac_status_name).delete()
        IPAddress.objects.create(address="1.1.1.1/32")
        self.assertTrue(IPAddress.objects.filter(address="1.1.1.1/32").exists())


class TestVLANGroup(ModelTestCases.BaseModelTestCase):
    model = VLANGroup

    def test_vlan_group_validation(self):
        location_type = LocationType.objects.get(name="Elevator")
        location = Location.objects.filter(location_type=location_type).first()
        group = VLANGroup(name="Group 1", location=location)
        with self.assertRaises(ValidationError) as cm:
            group.validated_save()
        self.assertIn(f'VLAN groups may not associate to locations of type "{location_type.name}"', str(cm.exception))

    def test_get_next_available_vid(self):
        vlangroup = VLANGroup.objects.create(name="VLAN Group 1", slug="vlan-group-1")
        VLAN.objects.bulk_create(
            (
                VLAN(name="VLAN 1", vid=1, vlan_group=vlangroup),
                VLAN(name="VLAN 2", vid=2, vlan_group=vlangroup),
                VLAN(name="VLAN 3", vid=3, vlan_group=vlangroup),
                VLAN(name="VLAN 5", vid=5, vlan_group=vlangroup),
            )
        )
        self.assertEqual(vlangroup.get_next_available_vid(), 4)

        VLAN.objects.bulk_create((VLAN(name="VLAN 4", vid=4, vlan_group=vlangroup),))
        self.assertEqual(vlangroup.get_next_available_vid(), 6)


class VLANTestCase(ModelTestCases.BaseModelTestCase):
    model = VLAN

    def test_vlan_validation(self):
        location_type = LocationType.objects.get(name="Root")
        location_type.content_types.set([])
        location_type.validated_save()
        location = Location.objects.filter(location_type=location_type).first()
        vlan = VLAN(name="Group 1", vid=1, location=location)
        vlan.status = Status.objects.get_for_model(VLAN).first()
        with self.assertRaises(ValidationError) as cm:
            vlan.validated_save()
        self.assertIn(f'VLANs may not associate to locations of type "{location_type.name}"', str(cm.exception))

        location_type.content_types.add(ContentType.objects.get_for_model(VLAN))
        group = VLANGroup.objects.create(name="Group 1")
        vlan.vlan_group = group
        location_2 = Location.objects.create(name="Location 2", location_type=location_type)
        group.location = location_2
        group.save()
        with self.assertRaises(ValidationError) as cm:
            vlan.validated_save()
        self.assertIn(
            f'The assigned group belongs to a location that does not include location "{location.name}"',
            str(cm.exception),
        )
