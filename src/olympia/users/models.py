import os
import re
import time
from contextlib import contextmanager
from datetime import datetime

from django import dispatch, forms
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.core import validators
from django.db import models, transaction
from django.template import Context, loader
from django.utils import timezone
from django.utils.translation import ugettext as _, get_language, activate
from django.utils.encoding import force_text
from django.utils.functional import lazy

import caching.base as caching
import commonware.log

from olympia import amo
from olympia.amo.models import OnChangeMixin, ManagerBase, ModelBase
from olympia.access.models import Group, GroupUser
from olympia.amo.urlresolvers import reverse
from olympia.translations.fields import NoLinksField, save_signal
from olympia.translations.models import Translation
from olympia.translations.query import order_by_translation

log = commonware.log.getLogger('z.users')


class UserForeignKey(models.ForeignKey):
    """
    A replacement for  models.ForeignKey('users.UserProfile').

    This field uses UserEmailField to make form fields key off the user's email
    instead of the primary key id.  We also hook up autocomplete automatically.
    """

    def __init__(self, *args, **kwargs):
        # "to" is passed here from the migration framework; we ignore it
        # since it's the same for every instance.
        kwargs.pop('to', None)
        self.to = 'users.UserProfile'
        super(UserForeignKey, self).__init__(self.to, *args, **kwargs)

    def value_from_object(self, obj):
        return getattr(obj, self.name).email

    def deconstruct(self):
        name, path, args, kwargs = super(UserForeignKey, self).deconstruct()
        kwargs['to'] = self.to
        return (name, path, args, kwargs)

    def formfield(self, **kw):
        defaults = {'form_class': UserEmailField}
        defaults.update(kw)
        return models.Field.formfield(self, **defaults)


class UserEmailField(forms.EmailField):

    def clean(self, value):
        if value in validators.EMPTY_VALUES:
            raise forms.ValidationError(self.error_messages['required'])
        try:
            return UserProfile.objects.get(email=value)
        except UserProfile.DoesNotExist:
            raise forms.ValidationError(_('No user with that email.'))

    def widget_attrs(self, widget):
        lazy_reverse = lazy(reverse, str)
        return {'class': 'email-autocomplete',
                'data-src': lazy_reverse('users.ajax')}


class UserManager(BaseUserManager, ManagerBase):

    def create_user(self, username, email, fxa_id=None):
        # We'll send username=None when registering through FxA to generate
        # an anonymous username.
        now = timezone.now()
        user = self.model(
            username=username, email=email, fxa_id=fxa_id,
            last_login=now)
        if username is None:
            user.anonymize_username()
        user.set_unusable_password()
        log.debug('Creating user with email {} and username {}'.format(
            email, username))
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email):
        """
        Creates and saves a superuser.
        """
        user = self.create_user(username, email)
        admins = Group.objects.get(name='Admins')
        GroupUser.objects.create(user=user, group=admins)
        return user


class UserProfile(OnChangeMixin, ModelBase, AbstractBaseUser):
    objects = UserManager()
    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['email']
    username = models.CharField(max_length=255, default='', unique=True)
    display_name = models.CharField(max_length=255, default='', null=True,
                                    blank=True)

    email = models.EmailField(unique=True, null=True, max_length=75)

    averagerating = models.CharField(max_length=255, blank=True, null=True)
    bio = NoLinksField(short=False)
    confirmationcode = models.CharField(max_length=255, default='',
                                        blank=True)
    deleted = models.BooleanField(default=False)
    display_collections = models.BooleanField(default=False)
    display_collections_fav = models.BooleanField(default=False)
    homepage = models.URLField(max_length=255, blank=True, default='')
    location = models.CharField(max_length=255, blank=True, default='')
    notes = models.TextField(blank=True, null=True)
    notifycompat = models.BooleanField(default=True)
    notifyevents = models.BooleanField(default=True)
    occupation = models.CharField(max_length=255, default='', blank=True)
    # This is essentially a "has_picture" flag right now
    picture_type = models.CharField(max_length=75, default='', blank=True)
    read_dev_agreement = models.DateTimeField(null=True, blank=True)

    last_login_ip = models.CharField(default='', max_length=45, editable=False)
    last_login_attempt = models.DateTimeField(null=True, editable=False)
    last_login_attempt_ip = models.CharField(default='', max_length=45,
                                             editable=False)
    failed_login_attempts = models.PositiveIntegerField(default=0,
                                                        editable=False)

    is_verified = models.BooleanField(default=True)
    region = models.CharField(max_length=11, null=True, blank=True,
                              editable=False)
    lang = models.CharField(max_length=5, null=True, blank=True,
                            default=settings.LANGUAGE_CODE)

    fxa_id = models.CharField(blank=True, null=True, max_length=128)

    class Meta:
        db_table = 'users'

    def __init__(self, *args, **kw):
        super(UserProfile, self).__init__(*args, **kw)
        if self.username:
            self.username = force_text(self.username)

    def __unicode__(self):
        return u'%s: %s' % (self.id, self.display_name or self.username)

    @property
    def is_superuser(self):
        return self.groups.filter(rules='*:*').exists()

    @property
    def is_staff(self):
        from olympia.access import acl
        return acl.action_allowed_user(self, 'Admin', '%')

    def has_perm(self, perm, obj=None):
        return self.is_superuser

    def has_module_perms(self, app_label):
        return self.is_superuser

    backend = 'django.contrib.auth.backends.ModelBackend'

    def is_anonymous(self):
        return False

    @staticmethod
    def create_user_url(id_, username=None, url_name='profile', src=None,
                        args=None):
        """
        We use <username> as the slug, unless it contains gross
        characters - in which case use <id> as the slug.
        """
        from olympia.amo.utils import urlparams
        chars = '/<>"\''
        if not username or any(x in chars for x in username):
            username = id_
        args = args or []
        url = reverse('users.%s' % url_name, args=[username] + args)
        return urlparams(url, src=src)

    def get_user_url(self, name='profile', src=None, args=None):
        return self.create_user_url(self.id, self.username, name, src, args)

    def get_url_path(self, src=None):
        return self.get_user_url('profile', src=src)

    @amo.cached_property(writable=True)
    def groups_list(self):
        """List of all groups the user is a member of, as a cached property."""
        return list(self.groups.all())

    @amo.cached_property
    def addons_listed(self):
        """Public add-ons this user is listed as author of."""
        return self.addons.reviewed().filter(
            addonuser__user=self, addonuser__listed=True)

    @property
    def num_addons_listed(self):
        """Number of public add-ons this user is listed as author of."""
        return self.addons.reviewed().filter(
            addonuser__user=self, addonuser__listed=True).count()

    def my_addons(self, n=8, with_unlisted=False):
        """Returns n addons"""
        addons = self.addons
        if with_unlisted:
            addons = self.addons.model.with_unlisted.filter(authors=self)
        qs = order_by_translation(addons, 'name')
        return qs[:n]

    @property
    def picture_dir(self):
        from olympia.amo.helpers import user_media_path
        split_id = re.match(r'((\d*?)(\d{0,3}?))\d{1,3}$', str(self.id))
        return os.path.join(user_media_path('userpics'),
                            split_id.group(2) or '0',
                            split_id.group(1) or '0')

    @property
    def picture_path(self):
        return os.path.join(self.picture_dir, str(self.id) + '.png')

    @property
    def picture_url(self):
        from olympia.amo.helpers import user_media_url
        if not self.picture_type:
            return settings.STATIC_URL + '/img/zamboni/anon_user.png'
        else:
            split_id = re.match(r'((\d*?)(\d{0,3}?))\d{1,3}$', str(self.id))
            modified = int(time.mktime(self.modified.timetuple()))
            path = "/".join([
                split_id.group(2) or '0',
                split_id.group(1) or '0',
                "%s.png?modified=%s" % (self.id, modified)
            ])
            return user_media_url('userpics') + path

    @amo.cached_property
    def is_developer(self):
        return self.addonuser_set.exists()

    @amo.cached_property
    def is_addon_developer(self):
        return self.addonuser_set.exclude(
            addon__type=amo.ADDON_PERSONA).exists()

    @amo.cached_property
    def is_artist(self):
        """Is this user a Personas Artist?"""
        return self.addonuser_set.filter(
            addon__type=amo.ADDON_PERSONA).exists()

    @property
    def name(self):
        if self.display_name:
            return force_text(self.display_name)
        elif self.has_anonymous_username():
            # L10n: {id} will be something like "13ad6a", just a random number
            # to differentiate this user from other anonymous users.
            return _('Anonymous user {id}').format(
                id=self._anonymous_username_id())
        else:
            return force_text(self.username)

    welcome_name = name

    def get_full_name(self):
        return self.name

    def _anonymous_username_id(self):
        if self.has_anonymous_username():
            return self.username.split('-')[1][:6]

    def anonymize_username(self):
        """Set an anonymous username."""
        if self.pk:
            log.info('Anonymizing username for {}'.format(self.pk))
        else:
            log.info('Generating username for {}'.format(self.email))
        self.username = 'anonymous-{}'.format(os.urandom(16).encode('hex'))
        return self.username

    def has_anonymous_username(self):
        return re.match('^anonymous-[0-9a-f]{32}$', self.username)

    def has_anonymous_display_name(self):
        return not self.display_name and self.has_anonymous_username()

    @amo.cached_property
    def reviews(self):
        """All reviews that are not dev replies."""
        qs = self._reviews_all.filter(reply_to=None)
        # Force the query to occur immediately. Several
        # reviews-related tests hang if this isn't done.
        return qs

    def anonymize(self):
        log.info(u"User (%s: <%s>) is being anonymized." % (self, self.email))
        self.email = None
        self.set_unusable_password()
        self.fxa_id = None
        self.username = "Anonymous-%s" % self.id  # Can't be null
        self.display_name = None
        self.homepage = ""
        self.deleted = True
        self.picture_type = ""
        self.save()

    @transaction.atomic
    def restrict(self):
        from olympia.amo.utils import send_mail
        log.info(u'User (%s: <%s>) is being restricted and '
                 'its user-generated content removed.' % (self, self.email))
        g = Group.objects.get(rules='Restricted:UGC')
        GroupUser.objects.create(user=self, group=g)
        self.reviews.all().delete()
        self.collections.all().delete()

        t = loader.get_template('users/email/restricted.ltxt')
        send_mail(_('Your account has been restricted'),
                  t.render(Context({})), None, [self.email],
                  use_blacklist=False)

    def unrestrict(self):
        log.info(u'User (%s: <%s>) is being unrestricted.' % (self,
                                                              self.email))
        GroupUser.objects.filter(user=self,
                                 group__rules='Restricted:UGC').delete()

    def set_unusable_password(self):
        self.password = ''

    def set_password(self, password):
        raise NotImplementedError('cannot set password')

    def check_password(self, password):
        raise NotImplementedError('cannot check password')

    def log_login_attempt(self, successful):
        """Log a user's login attempt"""
        self.last_login_attempt = datetime.now()
        self.last_login_attempt_ip = commonware.log.get_remote_addr()

        if successful:
            log.debug(u"User (%s) logged in successfully" % self)
            self.failed_login_attempts = 0
            self.last_login_ip = commonware.log.get_remote_addr()
        else:
            log.debug(u"User (%s) failed to log in" % self)
            if self.failed_login_attempts < 16777216:
                self.failed_login_attempts += 1

        self.save(update_fields=['last_login_ip', 'last_login_attempt',
                                 'last_login_attempt_ip',
                                 'failed_login_attempts'])

    def mobile_collection(self):
        return self.special_collection(
            amo.COLLECTION_MOBILE,
            defaults={'slug': 'mobile', 'listed': False,
                      'name': _('My Mobile Add-ons')})

    def favorites_collection(self):
        return self.special_collection(
            amo.COLLECTION_FAVORITES,
            defaults={'slug': 'favorites', 'listed': False,
                      'name': _('My Favorite Add-ons')})

    def special_collection(self, type_, defaults):
        from olympia.bandwagon.models import Collection
        c, new = Collection.objects.get_or_create(
            author=self, type=type_, defaults=defaults)
        if new:
            # Do an extra query to make sure this gets transformed.
            c = Collection.objects.using('default').get(id=c.id)
        return c

    @contextmanager
    def activate_lang(self):
        """
        Activate the language for the user. If none is set will go to the site
        default which is en-US.
        """
        lang = self.lang if self.lang else settings.LANGUAGE_CODE
        old = get_language()
        activate(lang)
        yield
        activate(old)

    def remove_locale(self, locale):
        """Remove the given locale for the user."""
        Translation.objects.remove_for(self, locale)

    @classmethod
    def get_fallback(cls):
        return cls._meta.get_field('lang')

    def addons_for_collection_type(self, type_):
        """Return the addons for the given special collection type."""
        from olympia.bandwagon.models import CollectionAddon
        qs = CollectionAddon.objects.filter(
            collection__author=self, collection__type=type_)
        return qs.values_list('addon', flat=True)

    @amo.cached_property
    def mobile_addons(self):
        return self.addons_for_collection_type(amo.COLLECTION_MOBILE)

    @amo.cached_property
    def favorite_addons(self):
        return self.addons_for_collection_type(amo.COLLECTION_FAVORITES)

    @amo.cached_property
    def watching(self):
        return self.collectionwatcher_set.values_list('collection', flat=True)


models.signals.pre_save.connect(save_signal, sender=UserProfile,
                                dispatch_uid='userprofile_translations')


@dispatch.receiver(models.signals.post_save, sender=UserProfile,
                   dispatch_uid='user.post_save')
def user_post_save(sender, instance, **kw):
    if not kw.get('raw'):
        from . import tasks
        tasks.index_users.delay([instance.id])


@dispatch.receiver(models.signals.post_delete, sender=UserProfile,
                   dispatch_uid='user.post_delete')
def user_post_delete(sender, instance, **kw):
    if not kw.get('raw'):
        from . import tasks
        tasks.unindex_users.delay([instance.id])


class UserNotification(ModelBase):
    user = models.ForeignKey(UserProfile, related_name='notifications')
    notification_id = models.IntegerField()
    enabled = models.BooleanField(default=False)

    class Meta:
        db_table = 'users_notifications'

    @staticmethod
    def update_or_create(update=None, **kwargs):
        if update is None:
            update = {}
        rows = UserNotification.objects.filter(**kwargs).update(**update)
        if not rows:
            update.update(dict(**kwargs))
            UserNotification.objects.create(**update)


class BlacklistedName(ModelBase):
    """Blacklisted User usernames and display_names + Collections' names."""
    name = models.CharField(max_length=255, unique=True, default='')

    class Meta:
        db_table = 'users_blacklistedname'

    def __unicode__(self):
        return self.name

    @classmethod
    def blocked(cls, name):
        """
        Check to see if a given name is in the (cached) blacklist.
        Return True if the name contains one of the blacklisted terms.

        """
        name = name.lower()
        qs = cls.objects.all()

        def f():
            return [n.lower() for n in qs.values_list('name', flat=True)]

        blacklist = caching.cached_with(qs, f, 'blocked')
        return any(n in name for n in blacklist)


class UserHistory(ModelBase):
    email = models.EmailField(max_length=75)
    user = models.ForeignKey(UserProfile, related_name='history')

    class Meta:
        db_table = 'users_history'
        ordering = ('-created',)


@UserProfile.on_change
def watch_email(old_attr=None, new_attr=None, instance=None,
                sender=None, **kw):
    if old_attr is None:
        old_attr = {}
    if new_attr is None:
        new_attr = {}
    new_email, old_email = new_attr.get('email'), old_attr.get('email')
    if old_email and new_email != old_email:
        log.debug('Creating user history for user: %s' % instance.pk)
        UserHistory.objects.create(email=old_email, user_id=instance.pk)
