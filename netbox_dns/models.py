import ipaddress

from math import ceil
from datetime import datetime

import dns
from dns import rdata, rdatatype, rdataclass
from dns.rdtypes.ANY import SOA

from netaddr import IPNetwork, AddrFormatError, IPAddress

from django.core.validators import (
    MinValueValidator,
    MaxValueValidator,
    validate_ipv6_address,
    validate_ipv4_address,
)

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import models, transaction
from django.db.models import Q, Max, ExpressionWrapper, BooleanField
from django.db.models.functions import Length
from django.urls import reverse

from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from utilities.querysets import RestrictedQuerySet
from utilities.choices import ChoiceSet

from netbox.models import NetBoxModel

from netbox_dns.fields import NetworkField


class NameServer(NetBoxModel):
    name = models.CharField(
        unique=True,
        max_length=255,
    )
    description = models.CharField(
        max_length=200,
        blank=True,
    )

    clone_fields = ["name", "description"]

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return str(self.name)

    def get_absolute_url(self):
        return reverse("plugins:netbox_dns:nameserver", kwargs={"pk": self.pk})


class ZoneManager(models.Manager.from_queryset(RestrictedQuerySet)):
    """Special Manager for zones providing the activity status annotation"""

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .annotate(
                active=ExpressionWrapper(
                    Q(status__in=Zone.ACTIVE_STATUS_LIST), output_field=BooleanField()
                )
            )
        )


class ZoneStatusChoices(ChoiceSet):
    key = "Zone.status"

    STATUS_ACTIVE = "active"
    STATUS_RESERVED = "reserved"
    STATUS_DEPRECATED = "deprecated"
    STATUS_PARKED = "parked"

    CHOICES = [
        (STATUS_ACTIVE, "Active", "blue"),
        (STATUS_RESERVED, "Reserved", "cyan"),
        (STATUS_DEPRECATED, "Deprecated", "red"),
        (STATUS_PARKED, "Parked", "gray"),
    ]


class Zone(NetBoxModel):
    ACTIVE_STATUS_LIST = (ZoneStatusChoices.STATUS_ACTIVE,)

    view = models.ForeignKey(
        to="View",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
    )
    name = models.CharField(
        max_length=255,
    )
    status = models.CharField(
        max_length=50,
        choices=ZoneStatusChoices,
        default=ZoneStatusChoices.STATUS_ACTIVE,
        blank=True,
    )
    nameservers = models.ManyToManyField(
        NameServer,
        related_name="zones",
        blank=True,
    )
    default_ttl = models.PositiveIntegerField(
        blank=True,
        verbose_name="Default TTL",
        validators=[MinValueValidator(1)],
    )
    soa_ttl = models.PositiveIntegerField(
        blank=False,
        null=False,
        verbose_name="SOA TTL",
        validators=[MinValueValidator(1)],
    )
    soa_mname = models.ForeignKey(
        NameServer,
        related_name="zones_soa",
        verbose_name="SOA MName",
        on_delete=models.PROTECT,
        blank=False,
        null=False,
    )
    soa_rname = models.CharField(
        max_length=255,
        blank=False,
        null=False,
        verbose_name="SOA RName",
    )
    soa_serial = models.BigIntegerField(
        blank=True,
        null=True,
        verbose_name="SOA Serial",
        validators=[MinValueValidator(1), MaxValueValidator(4294967295)],
    )
    soa_refresh = models.PositiveIntegerField(
        blank=False,
        null=False,
        verbose_name="SOA Refresh",
        validators=[MinValueValidator(1)],
    )
    soa_retry = models.PositiveIntegerField(
        blank=False,
        null=False,
        verbose_name="SOA Retry",
        validators=[MinValueValidator(1)],
    )
    soa_expire = models.PositiveIntegerField(
        blank=False,
        null=False,
        verbose_name="SOA Expire",
        validators=[MinValueValidator(1)],
    )
    soa_minimum = models.PositiveIntegerField(
        blank=False,
        null=False,
        verbose_name="SOA Minimum TTL",
        validators=[MinValueValidator(1)],
    )
    soa_serial_auto = models.BooleanField(
        verbose_name="Generate SOA Serial",
        help_text="Automatically generate the SOA Serial field",
        default=True,
    )
    description = models.CharField(
        max_length=200,
        blank=True,
    )
    arpa_network = NetworkField(
        verbose_name="ARPA Network",
        help_text="Network related to a reverse lookup zone (.arpa)",
        blank=True,
        null=True,
    )

    objects = ZoneManager()

    clone_fields = [
        "view",
        "name",
        "status",
        "nameservers",
        "default_ttl",
        "soa_ttl",
        "soa_mname",
        "soa_rname",
        "soa_refresh",
        "soa_retry",
        "soa_expire",
        "soa_minimum",
        "description",
    ]

    class Meta:
        ordering = (
            "view",
            "name",
        )
        unique_together = (
            "view",
            "name",
        )

    def __str__(self):
        if self.view:
            return f"[{self.view}] {self.name}"

        return str(self.name)

    def get_status_color(self):
        return ZoneStatusChoices.colors.get(self.status)

    def get_absolute_url(self):
        return reverse("plugins:netbox_dns:zone", kwargs={"pk": self.pk})

    def get_status_class(self):
        return self.CSS_CLASSES.get(self.status)

    @property
    def is_active(self):
        return self.status in Zone.ACTIVE_STATUS_LIST

    @property
    def is_reverse_zone(self):
        return self.name.endswith(".arpa")

    @property
    def view_filter(self):
        if self.view is None:
            return Q(view__isnull=True)
        return Q(view=self.view)

    def update_soa_record(self):
        soa_name = "@"
        soa_ttl = self.soa_ttl
        soa_rdata = dns.rdtypes.ANY.SOA.SOA(
            rdclass=RecordClassChoices.IN,
            rdtype=RecordTypeChoices.SOA,
            mname=self.soa_mname.name,
            rname=self.soa_rname,
            serial=self.soa_serial,
            refresh=self.soa_refresh,
            retry=self.soa_retry,
            expire=self.soa_expire,
            minimum=self.soa_minimum,
        )

        try:
            soa_record = self.record_set.get(type=RecordTypeChoices.SOA, name=soa_name)

            if soa_record.ttl != soa_ttl or soa_record.value != soa_rdata.to_text():
                soa_record.ttl = soa_ttl
                soa_record.value = soa_rdata.to_text()
                soa_record.managed = True
                soa_record.save()

        except Record.DoesNotExist:
            Record.objects.create(
                zone_id=self.pk,
                type=RecordTypeChoices.SOA,
                name=soa_name,
                ttl=soa_ttl,
                value=soa_rdata.to_text(),
                managed=True,
            )

    def update_ns_records(self, nameservers):
        ns_name = "@"
        ns_ttl = self.default_ttl

        delete_ns = self.record_set.filter(
            type=RecordTypeChoices.NS, managed=True
        ).exclude(value__in=nameservers)
        for record in delete_ns:
            record.delete()

        for ns in nameservers:
            Record.raw_objects.update_or_create(
                zone_id=self.pk,
                type=RecordTypeChoices.NS,
                name=ns_name,
                ttl=ns_ttl,
                value=ns,
                managed=True,
            )

    def check_nameservers(self):
        nameservers = self.nameservers.all()

        ns_warnings = []
        ns_errors = []

        if not nameservers:
            ns_errors.append(f"No nameservers are configured for zone {self}")

        for nameserver in nameservers:
            ns_domain = ".".join(nameserver.name.split(".")[1:])
            if not ns_domain:
                continue

            view_condition = (
                Q(view__isnull=True) if self.view is None else Q(view_id=self.view.pk)
            )

            try:
                ns_zone = Zone.objects.get(view_condition, name=ns_domain)
            except ObjectDoesNotExist:
                continue

            ns_name = nameserver.name.split(".")[0]
            address_records = Record.objects.filter(
                Q(zone=ns_zone),
                Q(status__in=Record.ACTIVE_STATUS_LIST),
                Q(Q(name=f"{nameserver.name}.") | Q(name=ns_name)),
                Q(Q(type=RecordTypeChoices.A) | Q(type=RecordTypeChoices.AAAA)),
            )

            if not address_records:
                ns_warnings.append(
                    f"Nameserver {nameserver.name} does not have an active address record in zone {ns_zone}"
                )

        return ns_warnings, ns_errors

    def get_auto_serial(self):
        records = Record.objects.filter(zone=self).exclude(type=RecordTypeChoices.SOA)
        if records:
            soa_serial = (
                records.aggregate(Max("last_updated"))
                .get("last_updated__max")
                .timestamp()
            )
        else:
            soa_serial = ceil(datetime.now().timestamp())

        if self.last_updated:
            soa_serial = ceil(max(soa_serial, self.last_updated.timestamp()))

        return soa_serial

    def update_serial(self):
        self.last_updated = datetime.now()
        self.soa_serial = ceil(datetime.now().timestamp())
        self.update_soa_record()
        super().save()

    def get_network(self):
        name = self.name.rstrip(".")

        if name.endswith(".in-addr.arpa"):
            address = ".".join(reversed(name.replace(".in-addr.arpa", "").split(".")))
            mask = len(address.split(".")) * 8

            try:
                return IPNetwork(f"{address}/{mask}")
            except AddrFormatError:
                return None

        elif name.endswith("ip6.arpa"):
            address = "".join(reversed(name.replace(".ip6.arpa", "").split(".")))
            mask = len(address)
            address = address + "0" * (32 - mask)

            try:
                return IPNetwork(
                    f"{':'.join([(address[i:i+4]) for i in range(0, mask, 4)])}::/{mask*4}"
                )
            except AddrFormatError:
                return None

        else:
            return None

    def check_name_conflict(self):
        if self.view is None:
            if (
                Zone.objects.exclude(pk=self.pk)
                .filter(name=self.name, view__isnull=True)
                .exists()
            ):
                raise ValidationError(
                    {
                        "name": f"A zone with name {self.name} and no view already exists."
                    }
                )

    def clean(self, *args, **kwargs):
        self.check_name_conflict()

        if self.soa_serial is None and not self.soa_serial_auto:
            raise ValidationError(
                {
                    "soa_serial": f"soa_serial is not defined and soa_serial_auto is disabled for zone {self.name}."
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean()

        new_zone = self.pk is None
        if not new_zone:
            old_zone = Zone.objects.get(pk=self.pk)

        name_changed = not new_zone and old_zone.name != self.name
        view_changed = not new_zone and old_zone.view != self.view
        status_changed = not new_zone and old_zone.status != self.status

        if self.soa_serial_auto:
            self.soa_serial = self.get_auto_serial()

        if self.is_reverse_zone:
            self.arpa_network = self.get_network()

        super().save(*args, **kwargs)

        if (
            new_zone or name_changed or view_changed or status_changed
        ) and self.is_reverse_zone:
            zones = Zone.objects.filter(
                self.view_filter,
                arpa_network__net_contains_or_equals=self.arpa_network,
            )
            address_records = Record.objects.filter(
                Q(ptr_record__isnull=True) | Q(ptr_record__zone__in=zones),
                type__in=(RecordTypeChoices.A, RecordTypeChoices.AAAA),
                disable_ptr=False,
            )
            for record in address_records:
                record.update_ptr_record()

        elif name_changed or view_changed or status_changed:
            for record in self.record_set.filter(
                type__in=(RecordTypeChoices.A, RecordTypeChoices.AAAA)
            ):
                record.update_ptr_record()

        self.update_soa_record()

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            address_records = list(self.record_set.filter(ptr_record__isnull=False))
            for record in address_records:
                record.ptr_record.delete()

            ptr_records = self.record_set.filter(address_record__isnull=False)
            update_records = [
                record.pk
                for record in Record.objects.filter(ptr_record__in=ptr_records)
            ]

            super().delete(*args, **kwargs)

        for record in Record.objects.filter(pk__in=update_records):
            record.update_ptr_record()


@receiver(m2m_changed, sender=Zone.nameservers.through)
def update_ns_records(**kwargs):
    if kwargs.get("action") not in ["post_add", "post_remove"]:
        return

    zone = kwargs.get("instance")
    nameservers = zone.nameservers.all()

    new_nameservers = [f'{ns.name.rstrip(".")}.' for ns in nameservers]

    zone.update_ns_records(new_nameservers)


class RecordManager(models.Manager.from_queryset(RestrictedQuerySet)):
    """Special Manager for records providing the activity status annotation"""

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .annotate(
                active=ExpressionWrapper(
                    Q(
                        Q(zone__status__in=Zone.ACTIVE_STATUS_LIST)
                        & Q(
                            Q(address_record__isnull=True)
                            | Q(
                                address_record__zone__status__in=Zone.ACTIVE_STATUS_LIST
                            )
                        )
                        & Q(status__in=Record.ACTIVE_STATUS_LIST)
                    ),
                    output_field=BooleanField(),
                )
            )
        )


def initialize_choice_names(cls):
    for choice in cls.CHOICES:
        setattr(cls, choice[0], choice[0])
    return cls


@initialize_choice_names
class RecordTypeChoices(ChoiceSet):
    CHOICES = [
        (rdtype.name, rdtype.name)
        for rdtype in sorted(rdatatype.RdataType, key=lambda a: a.name)
        if not rdatatype.is_metatype(rdtype)
    ]
    SINGLETONS = [
        rdtype.name for rdtype in rdatatype.RdataType if rdatatype.is_singleton(rdtype)
    ]


@initialize_choice_names
class RecordClassChoices(ChoiceSet):
    CHOICES = [
        (rdclass.name, rdclass.name)
        for rdclass in sorted(rdataclass.RdataClass)
        if not rdataclass.is_metaclass(rdclass)
    ]


class RecordStatusChoices(ChoiceSet):
    key = "Record.status"

    STATUS_ACTIVE = "active"
    STATUS_INACTIVE = "inactive"

    CHOICES = [
        (STATUS_ACTIVE, "Active", "blue"),
        (STATUS_INACTIVE, "Inactive", "red"),
    ]


class Record(NetBoxModel):
    ACTIVE_STATUS_LIST = (RecordStatusChoices.STATUS_ACTIVE,)

    unique_ptr_qs = Q(
        Q(disable_ptr=False),
        Q(Q(type=RecordTypeChoices.A) | Q(type=RecordTypeChoices.AAAA)),
    )

    zone = models.ForeignKey(
        Zone,
        on_delete=models.CASCADE,
    )
    type = models.CharField(
        choices=RecordTypeChoices,
        max_length=10,
    )
    name = models.CharField(
        max_length=255,
    )
    value = models.CharField(
        max_length=1000,
    )
    status = models.CharField(
        max_length=50,
        choices=RecordStatusChoices,
        default=RecordStatusChoices.STATUS_ACTIVE,
        blank=False,
    )
    ttl = models.PositiveIntegerField(
        verbose_name="TTL",
        null=True,
        blank=True,
    )
    managed = models.BooleanField(
        null=False,
        default=False,
    )
    ptr_record = models.OneToOneField(
        "self",
        on_delete=models.SET_NULL,
        related_name="address_record",
        verbose_name="PTR record",
        null=True,
        blank=True,
    )
    disable_ptr = models.BooleanField(
        verbose_name="Disable PTR",
        help_text="Disable PTR record creation",
        default=False,
    )
    description = models.CharField(
        max_length=200,
        blank=True,
    )

    objects = RecordManager()
    raw_objects = RestrictedQuerySet.as_manager()

    clone_fields = [
        "zone",
        "type",
        "name",
        "value",
        "status",
        "ttl",
        "disable_ptr",
        "description",
    ]

    class Meta:
        ordering = ("zone", "name", "type", "value", "status")

    def __str__(self):
        if self.name == "@":
            return f"{self.zone.name} [{self.type}]"

        if self.name.endswith("."):
            return f"{self.name} [{self.type}]"

        return f"{self.name}.{self.zone.name} [{self.type}]"

    def get_status_color(self):
        return RecordStatusChoices.colors.get(self.status)

    def get_absolute_url(self):
        return reverse("plugins:netbox_dns:record", kwargs={"pk": self.id})

    def fqdn(self):
        return f"{self.name}.{self.zone.name}."

    @property
    def is_active(self):
        return (
            self.status in Record.ACTIVE_STATUS_LIST
            and self.zone.status in Zone.ACTIVE_STATUS_LIST
        )

    def ptr_zone(self):
        ptr_zones = Zone.objects.filter(
            self.zone.view_filter, arpa_network__net_contains=self.value
        ).order_by(Length("name").desc())

        if len(ptr_zones):
            return ptr_zones[0]

        return None

    def update_ptr_record(self):
        ptr_zone = self.ptr_zone()

        if ptr_zone is None or self.disable_ptr or not self.is_active:
            if self.ptr_record is not None:
                with transaction.atomic():
                    self.ptr_record.delete()
                    self.ptr_record = None
            return

        ptr_name = ipaddress.ip_address(self.value).reverse_pointer.replace(
            f".{ptr_zone.name}", ""
        )
        ptr_value = self.fqdn()
        ptr_record = self.ptr_record

        with transaction.atomic():
            if ptr_record is not None:
                if ptr_record.zone.pk != ptr_zone.pk:
                    ptr_record.delete()
                    ptr_record = None

                else:
                    if (
                        ptr_record.name != ptr_name
                        or ptr_record.value != ptr_value
                        or ptr_record.ttl != self.ttl
                    ):
                        ptr_record.name = ptr_name
                        ptr_record.value = ptr_value
                        ptr_record.ttl = self.ttl
                        ptr_record.save()

            if ptr_record is None:
                ptr_record = Record.objects.create(
                    zone_id=ptr_zone.pk,
                    type=RecordTypeChoices.PTR,
                    name=ptr_name,
                    ttl=self.ttl,
                    value=ptr_value,
                    managed=True,
                )

        self.ptr_record = ptr_record
        if self.pk:
            super().save()

    def clean_fields(self, *args, **kwargs):
        self.type = self.type.upper()
        super().clean_fields(*args, **kwargs)

    def clean(self, *args, **kwargs):
        ip_version = None

        try:
            if self.type == RecordTypeChoices.A:
                ip_version = "4"
                validate_ipv4_address(self.value)
            elif self.type == RecordTypeChoices.AAAA:
                ip_version = "6"
                validate_ipv6_address(self.value)
            else:
                rdata.from_text(RecordClassChoices.IN, self.type, self.value)

        except ValidationError:
            raise ValidationError(
                {
                    "value": f"A valid IPv{ip_version} address is required for record type {self.type}."
                }
            ) from None

        except dns.exception.SyntaxError as exc:
            raise ValidationError(
                {"value": f"Record value {self.value} is malformed: {exc}."}
            ) from None

        if not self.is_active:
            return

        records = (
            Record.objects.filter(name=self.name, zone=self.zone)
            .exclude(pk=self.pk)
            .exclude(active=False)
        )

        if self.type == RecordTypeChoices.CNAME:
            if records.exists():
                raise ValidationError(
                    {
                        "type": f"There is already an active record for name {self.name} in zone {self.zone}, CNAME is not allowed."
                    }
                ) from None

        elif records.filter(type=RecordTypeChoices.CNAME).exists():
            raise ValidationError(
                {
                    "type": f"There is already an active CNAME record for name {self.name} in zone {self.zone}, no other record allowed."
                }
            ) from None

        elif self.type in RecordTypeChoices.SINGLETONS:
            if records.filter(type=self.type).exists():
                raise ValidationError(
                    {
                        "type": f"There is already an active {self.type} record for name {self.name} in zone {self.zone}, more than one are not allowed."
                    }
                ) from None

    def save(self, *args, **kwargs):
        self.full_clean()

        if self.type in (RecordTypeChoices.A, RecordTypeChoices.AAAA):
            self.update_ptr_record()
        elif self.ptr_record is not None:
            self.ptr_record.delete()
            self.ptr_record = None

        super().save(*args, **kwargs)

        zone = self.zone
        if self.type != RecordTypeChoices.SOA and zone.soa_serial_auto:
            zone.update_serial()

    def delete(self, *args, **kwargs):
        if self.ptr_record:
            self.ptr_record.delete()

        super().delete(*args, **kwargs)

        zone = self.zone
        if zone.soa_serial_auto:
            zone.update_serial()


class View(NetBoxModel):
    name = models.CharField(
        unique=True,
        max_length=255,
    )
    description = models.CharField(
        max_length=200,
        blank=True,
    )

    clone_fields = ["name", "description"]

    def get_absolute_url(self):
        return reverse("plugins:netbox_dns:view", kwargs={"pk": self.id})

    def __str__(self):
        return str(self.name)

    class Meta:
        ordering = ("name",)
