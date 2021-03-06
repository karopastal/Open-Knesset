# encoding: utf-8
import re, logging, random, sys, traceback
from datetime import date, timedelta

from django.contrib.comments import Comment
from django.db import models, IntegrityError
from django.contrib.contenttypes import generic
from django import forms
from django.utils.translation import ugettext_lazy as _
from django.utils.safestring import mark_safe
from django.utils.html import escape
from django.core.cache import cache
from django.conf import settings
from django.contrib.auth.models import User

from tagging.models import Tag, TaggedItem
from tagging.forms import TagField
import voting
from tagging.utils import get_tag
from actstream import Action
from actstream.models import Follow

from laws import constants
from laws.constants import FIRST_KNESSET_START
from laws.enums import BillStages
from mks.models import Party, Knesset
from tagvotes.models import TagVote
from knesset.utils import slugify_name
from laws.vote_choices import (TYPE_CHOICES, BILL_STAGE_CHOICES,
                               BILL_AGRR_STAGES, BILL_STAGES)

from ok_tag.models import add_tags_to_related_objects
from django.db.models.signals import post_save

logger = logging.getLogger("open-knesset.laws.models")
VOTE_ACTION_TYPE_CHOICES = (
    (u'for', _('For')),
    (u'against', _('Against')),
    (u'abstain', _('Abstain')),
    (u'no-vote', _('No Vote')),
)

CONVERT_TO_DISCUSSION_HEADERS = ('להעביר את הנושא'.decode('utf8'), 'העברת הנושא'.decode('utf8'))


class CandidateListVotingStatistics(models.Model):
    candidates_list = models.OneToOneField('polyorg.CandidateList', related_name='voting_statistics')

    def votes_against_party_count(self):
        return VoteAction.objects.filter(member__id__in=self.candidates_list.member_ids, against_party=True).count()

    def votes_count(self):
        return VoteAction.objects.filter(member__id__in=self.candidates_list.member_ids).exclude(type='no-vote').count()

    def votes_per_seat(self):
        return round(float(self.votes_count()) / len(self.candidates_list.member_ids))

    def discipline(self):
        total_votes = self.votes_count()
        if total_votes:
            votes_against_party = self.votes_against_party_count()
            return round(100.0 * (total_votes - votes_against_party) / total_votes, 1)
        return _('N/A')


class PartyVotingStatistics(models.Model):
    party = models.OneToOneField('mks.Party', related_name='voting_statistics')

    def votes_against_party_count(self):
        d = Knesset.objects.current_knesset().start_date
        return VoteAction.objects.filter(
            vote__time__gt=d,
            member__current_party=self.party,
            against_party=True).count()

    def votes_count(self):
        d = Knesset.objects.current_knesset().start_date
        return VoteAction.objects.filter(
            member__current_party=self.party,
            vote__time__gt=d).exclude(type='no-vote').count()

    def votes_per_seat(self):
        return round(float(self.votes_count()) / self.party.number_of_seats, 1)

    def discipline(self):
        total_votes = self.votes_count()
        if total_votes:
            votes_against_party = self.votes_against_party_count()
            return round(100.0 * (total_votes - votes_against_party) /
                         total_votes, 1)
        return _('N/A')

    def coalition_discipline(self):  # if party is in opposition this actually
        # returns opposition_discipline
        d = Knesset.objects.current_knesset().start_date
        total_votes = self.votes_count()
        if total_votes:
            if self.party.is_coalition:
                votes_against_coalition = VoteAction.objects.filter(
                    vote__time__gt=d,
                    member__current_party=self.party,
                    against_coalition=True).count()
            else:
                votes_against_coalition = VoteAction.objects.filter(
                    vote__time__gt=d,
                    member__current_party=self.party,
                    against_opposition=True).count()
            return round(100.0 * (total_votes - votes_against_coalition) /
                         total_votes, 1)
        return _('N/A')

    def __unicode__(self):
        return "{}".format(self.party.name)


class MemberVotingStatistics(models.Model):
    member = models.OneToOneField('mks.Member', related_name='voting_statistics')

    def votes_against_party_count(self, from_date=None):
        if from_date:
            return VoteAction.objects.filter(member=self.member, against_party=True, vote__time__gt=from_date).count()
        return VoteAction.objects.filter(member=self.member, against_party=True).count()

    def votes_count(self, from_date=None):
        if from_date:
            return VoteAction.objects.filter(member=self.member, vote__time__gt=from_date).exclude(
                type='no-vote').count()
        vc = cache.get('votes_count_%d' % self.member.id)
        if not vc:
            vc = VoteAction.objects.filter(member=self.member).exclude(type='no-vote').count()
            cache.set('votes_count_%d' % self.member.id, vc, settings.LONG_CACHE_TIME)
        return vc

    def average_votes_per_month(self):
        if hasattr(self, '_average_votes_per_month'):
            return self._average_votes_per_month
        st = self.member.service_time()
        self._average_votes_per_month = (30.0 * self.votes_count() / st) if st else 0
        return self._average_votes_per_month

    def discipline(self, from_date=None):
        total_votes = self.votes_count(from_date)
        if total_votes <= 3:  # not enough data
            return None
        votes_against_party = self.votes_against_party_count(from_date)
        return round(100.0 * (total_votes - votes_against_party) / total_votes, 1)

    def coalition_discipline(self,
                             from_date=None):  # if party is in opposition this actually returns opposition_discipline
        total_votes = self.votes_count(from_date)
        if total_votes <= 3:  # not enough data
            return None
        if self.member.current_party.is_coalition:
            v = VoteAction.objects.filter(member=self.member, against_coalition=True)
        else:
            v = VoteAction.objects.filter(member=self.member, against_opposition=True)
        if from_date:
            v = v.filter(vote__time__gt=from_date)
        votes_against_coalition = v.count()
        return round(100.0 * (total_votes - votes_against_coalition) / total_votes, 1)

    def __unicode__(self):
        return "{}".format(self.member.name)


class VoteAction(models.Model):
    type = models.CharField(max_length=10, choices=VOTE_ACTION_TYPE_CHOICES)
    member = models.ForeignKey('mks.Member')
    party = models.ForeignKey('mks.Party')
    vote = models.ForeignKey('Vote', related_name='actions')
    against_party = models.BooleanField(default=False)
    against_coalition = models.BooleanField(default=False)
    against_opposition = models.BooleanField(default=False)
    against_own_bill = models.BooleanField(default=False)

    def __unicode__(self):
        return u"{} {} {}".format(self.member.name, self.type, self.vote.title)

    def save(self, **kwargs):
        if not self.party_id:
            party = self.member.party_at(self.vote.time.date())
            if party:
                self.party_id = party.id
        super(VoteAction, self).save(**kwargs)


class VoteManager(models.Manager):
    # TODO: add i18n to the types so we'd have
    #   {'law-approve': _('approve law'), ...
    VOTE_TYPES = {'law-approve': u'אישור החוק', 'second-call': u'קריאה שנייה', 'demurrer': u'הסתייגות',
                  'no-confidence': u'הצעת אי-אמון', 'pass-to-committee': u'להעביר את ',
                  'continuation': u'להחיל דין רציפות'}

    def filter_and_order(self, *args, **kwargs):
        qs = self.all()
        filter_kwargs = {}
        if kwargs.get('vtype') and kwargs['vtype'] != 'all':
            filter_kwargs['title__startswith'] = self.VOTE_TYPES[kwargs['vtype']]

        if filter_kwargs:
            qs = qs.filter(**filter_kwargs)

        # In dealing with 'tagged' we use an ugly workaround for the fact that generic relations
        # don't work as expected with annotations.
        # please read http://code.djangoproject.com/ticket/10461 before trying to change this code
        if kwargs.get('tagged'):
            if kwargs['tagged'] == 'false':
                qs = qs.exclude(tagged_items__isnull=False)
            elif kwargs['tagged'] != 'all':
                qs = qs.filter(tagged_items__tag__name=kwargs['tagged'])

        if kwargs.get('to_date'):
            qs = qs.filter(time__lte=kwargs['to_date'] + timedelta(days=1))

        if kwargs.get('from_date'):
            qs = qs.filter(time__gte=kwargs['from_date'])

        exclude_agendas = kwargs.get('exclude_agendas')
        if exclude_agendas:
            # exclude votes that are ascribed to any of the given agendas
            from agendas.models import AgendaVote
            qs = qs.exclude(id__in=AgendaVote.objects.filter(
                agenda__in=exclude_agendas).values('vote__id'))

        if 'order' in kwargs:
            if kwargs['order'] == 'controversy':
                qs = qs.order_by('-controversy')
            if kwargs['order'] == 'against-party':
                qs = qs.order_by('-against_party')
            if kwargs['order'] == 'votes':
                qs = qs.order_by('-votes_count')

        if kwargs.get('exclude_ascribed', False):  # exclude votes ascribed to
            # any bill.
            qs = qs.exclude(bills_pre_votes__isnull=False).exclude(
                bills_first__isnull=False).exclude(bill_approved__isnull=False)
        return qs


class Vote(models.Model):
    meeting_number = models.IntegerField(null=True, blank=True)
    vote_number = models.IntegerField(null=True, blank=True)
    src_id = models.IntegerField(null=True, blank=True)
    src_url = models.URLField(max_length=1024, null=True, blank=True)
    title = models.CharField(max_length=1000)
    vote_type = models.CharField(max_length=32, choices=TYPE_CHOICES,
                                 blank=True)
    time = models.DateTimeField(db_index=True)
    time_string = models.CharField(max_length=100)
    votes = models.ManyToManyField('mks.Member', related_name='votes', blank=True, through='VoteAction')
    votes_count = models.IntegerField(null=True, blank=True)
    for_votes_count = models.IntegerField(null=True, blank=True)
    against_votes_count = models.IntegerField(null=True, blank=True)
    abstain_votes_count = models.IntegerField(null=True, blank=True)
    importance = models.FloatField(default=0.0)
    controversy = models.IntegerField(null=True, blank=True)
    against_party = models.IntegerField(null=True, blank=True)
    against_coalition = models.IntegerField(null=True, blank=True)
    against_opposition = models.IntegerField(null=True, blank=True)
    against_own_bill = models.IntegerField(null=True, blank=True)
    summary = models.TextField(null=True, blank=True)
    full_text = models.TextField(null=True, blank=True)
    full_text_url = models.URLField(max_length=1024, null=True, blank=True)

    tagged_items = generic.GenericRelation(TaggedItem,
                                           object_id_field="object_id",
                                           content_type_field="content_type")

    objects = VoteManager()

    class Meta:
        ordering = ('-time', '-id')
        verbose_name = _('Vote')
        verbose_name_plural = _('Votes')

    def __unicode__(self):
        return "%s (%s)" % (self.title, self.time_string)

    @property
    def passed(self):
        return self.for_votes_count > self.against_votes_count

    def get_voters_id(self, vote_type):
        return VoteAction.objects.filter(vote=self,
                                         type=vote_type).values_list('member__id', flat=True)

    def for_votes(self):
        return self.actions.select_related().filter(type='for')

    def against_votes(self):
        return self.actions.select_related().filter(type='against')

    def abstain_votes(self):
        return self.actions.select_related().filter(type='abstain')

    def against_party_votes(self):
        return self.votes.filter(voteaction__against_party=True)

    def against_coalition_votes(self):
        return self.votes.filter(voteaction__against_coalition=True)

    def against_own_bill_votes(self):
        return self.votes.filter(voteaction__against_own_bill=True)

    def _vote_type(self):
        if type(self.title) == str:
            f = str.decode
        else:  # its already unicode, do nothing
            f = lambda x, y: x
        for vtype, vtype_prefix in VoteManager.VOTE_TYPES.iteritems():
            if f(self.title, 'utf8').startswith(vtype_prefix):
                return vtype
        return ''

    def short_summary(self):
        if self.summary is None:
            return ''
        return self.summary[:60]

    def full_text_link(self):
        if self.full_text_url is None:
            return ''
        return '<a href="{}">link</a>'.format(self.full_text_url)

    full_text_link.allow_tags = True

    def bills(self):
        """Return a list of all bills related to this vote"""
        result = list(self.bills_pre_votes.all())
        result.extend(self.bills_first.all())
        b = Bill.objects.filter(approval_vote=self)
        if b:
            result.extend(b)
        return result

    @models.permalink
    def get_absolute_url(self):
        return ('vote-detail', [str(self.id)])

    def _get_tags(self):
        tags = Tag.objects.get_for_object(self)
        return tags

    def _set_tags(self, tag_list):
        Tag.objects.update_tags(self, tag_list)

    tags = property(_get_tags, _set_tags)

    def tags_with_user_votes(self, user):
        tags = Tag.objects.get_for_object(self)
        for t in tags:
            ti = TaggedItem.objects.filter(tag=t).filter(object_id=self.id)[0]
            t.score = sum(TagVote.objects.filter(tagged_item=ti).values_list('vote', flat=True))
            v = TagVote.objects.filter(tagged_item=ti).filter(user=user)
            if v:
                t.user_score = v[0].vote
            else:
                t.user_score = 0
        return tags.sorted(cmp=lambda x, y: cmp(x.score, y.score))

    def tag_form(self):
        tf = TagForm()
        tf.tags = self.tags
        tf.initial = {'tags': ', '.join([str(t) for t in self.tags])}
        return tf

    def update_vote_properties(self):
        # TODO: this can be heavily optimized. somewhere sometimes..
        party_ids = Party.objects.values_list('id', flat=True)
        d = self.time.date()
        party_is_coalition = dict(zip(
            party_ids,
            [x.is_coalition_at(self.time.date())
             for x in Party.objects.all()]
        ))

        def party_at_or_error(member, vote_date):
            party = member.party_at(vote_date)
            if party:
                return party
            else:
                raise Exception(
                    'could not find which party member %s belonged to during vote %s' % (member.pk, self.pk))

        for_party_ids = [party_at_or_error(va.member, vote_date=d).id for va in self.for_votes()]
        party_for_votes = [sum([x == party_id for x in for_party_ids]) for party_id in party_ids]

        against_party_ids = [party_at_or_error(va.member, vote_date=d).id for va in self.against_votes()]
        party_against_votes = [sum([x == party_id for x in against_party_ids]) for party_id in party_ids]

        party_stands_for = [float(fv) > constants.STANDS_FOR_THRESHOLD * (fv + av) for (fv, av) in
                            zip(party_for_votes, party_against_votes)]
        party_stands_against = [float(av) > constants.STANDS_FOR_THRESHOLD * (fv + av) for (fv, av) in
                                zip(party_for_votes, party_against_votes)]

        party_stands_for = dict(zip(party_ids, party_stands_for))
        party_stands_against = dict(zip(party_ids, party_stands_against))

        coalition_for_votes = sum([x for (x, y) in zip(party_for_votes, party_ids) if party_is_coalition[y]])
        coalition_against_votes = sum([x for (x, y) in zip(party_against_votes, party_ids) if party_is_coalition[y]])
        opposition_for_votes = sum([x for (x, y) in zip(party_for_votes, party_ids) if not party_is_coalition[y]])
        opposition_against_votes = sum(
            [x for (x, y) in zip(party_against_votes, party_ids) if not party_is_coalition[y]])

        coalition_total_votes = coalition_for_votes + coalition_against_votes
        coalition_stands_for = (
            float(coalition_for_votes) > constants.STANDS_FOR_THRESHOLD * (coalition_total_votes))
        coalition_stands_against = float(coalition_against_votes) > constants.STANDS_FOR_THRESHOLD * (
            coalition_total_votes)
        opposition_total_votes = opposition_for_votes + opposition_against_votes
        opposition_stands_for = float(opposition_for_votes) > constants.STANDS_FOR_THRESHOLD * opposition_total_votes
        opposition_stands_against = float(
            opposition_against_votes) > constants.STANDS_FOR_THRESHOLD * opposition_total_votes

        # a set of all MKs that proposed bills this vote is about.
        proposers = [set(b.proposers.all()) for b in self.bills()]
        if proposers:
            proposers = reduce(lambda x, y: set.union(x, y), proposers)

        against_party_count = 0
        against_coalition_count = 0
        against_opposition_count = 0
        against_own_bill_count = 0
        for va in VoteAction.objects.filter(vote=self):
            va.against_party = False
            va.against_coalition = False
            va.against_opposition = False
            va.against_own_bill = False
            voting_member_party_at_vote = party_at_or_error(va.member, vote_date=d)
            vote_action_member_party_id = voting_member_party_at_vote.id
            if party_stands_for[vote_action_member_party_id] and va.type == 'against':
                va.against_party = True
                against_party_count += 1
            if party_stands_against[vote_action_member_party_id] and va.type == 'for':
                va.against_party = True
                against_party_count += 1
            if voting_member_party_at_vote.is_coalition_at(self.time.date()):
                if (coalition_stands_for and va.type == 'against') or (coalition_stands_against and va.type == 'for'):
                    va.against_coalition = True
                    against_coalition_count += 1
            else:
                if (opposition_stands_for and va.type == 'against') or (opposition_stands_against and va.type == 'for'):
                    va.against_opposition = True
                    against_opposition_count += 1

            if va.member in proposers and va.type == 'against':
                va.against_own_bill = True
                against_own_bill_count += 1

            va.save()

        self.against_party = against_party_count
        self.against_coalition = against_coalition_count
        self.against_opposition = against_opposition_count
        self.against_own_bill = against_own_bill_count
        self.votes_count = VoteAction.objects.filter(vote=self).count()
        self.for_votes_count = VoteAction.objects.filter(vote=self, type='for').count()
        self.against_votes_count = VoteAction.objects.filter(vote=self, type='against').count()
        self.abstain_votes_count = VoteAction.objects.filter(vote=self, type='abstain').count()
        self.controversy = min(self.for_votes_count or 0,
                               self.against_votes_count or 0)
        self.vote_type = self._vote_type()
        self.save()

    def redownload_votes_page(self):
        from simple.management.commands.syncdata import Command as SyncdataCommand
        (page, vote_src_url) = SyncdataCommand().read_votes_page(self.src_id)
        return page

    def reparse_members_from_votes_page(self, page=None):
        from simple.management.commands.syncdata import Command as SyncdataCommand
        page = self.redownload_votes_page() if page is None else page
        syncdata = SyncdataCommand()
        results = syncdata.read_member_votes(page, return_ids=True)
        for (voter_id, voter_party, vote) in results:
            try:
                m = Member.objects.get(pk=int(voter_id))
            except:
                exceptionType, exceptionValue, exceptionTraceback = sys.exc_info()
                logger.error("%svoter_id = %s",
                             ''.join(traceback.format_exception(exceptionType, exceptionValue, exceptionTraceback)),
                             str(voter_id))
                continue
            va, created = VoteAction.objects.get_or_create(vote=self, member=m,
                                                           defaults={'type': vote, 'party': m.current_party})
            if created:
                va.save()


class TagForm(forms.Form):
    tags = TagField()


class Law(models.Model):
    title = models.CharField(max_length=1000)
    merged_into = models.ForeignKey('Law', related_name='duplicates', blank=True, null=True)

    def __unicode__(self):
        return self.title

    def merge(self, another_law):
        """
        Merges another_law into this one.
        Move all pointers from another_law to self,
        Then mark another_law as deleted by setting its merged_into field to self.
        """
        if another_law is self:
            return  # don't accidentally delete myself by trying to merge.
        for pp in another_law.laws_privateproposal_related.all():
            pp.law = self
            pp.save()
        for kp in another_law.laws_knessetproposal_related.all():
            kp.law = self
            kp.save()
        for bill in another_law.bills.all():
            bill.law = self
            bill.save()
        another_law.merged_into = self
        another_law.save()


class BillProposal(models.Model):
    knesset_id = models.IntegerField(blank=True, null=True)
    law = models.ForeignKey('Law', related_name="%(app_label)s_%(class)s_related", blank=True, null=True)
    title = models.CharField(max_length=1000)
    date = models.DateField(blank=True, null=True)
    source_url = models.URLField(max_length=1024, null=True, blank=True)
    content_html = models.TextField(blank=True, default="")
    committee_meetings = models.ManyToManyField('committees.CommitteeMeeting',
                                                related_name="%(app_label)s_%(class)s_related", blank=True, null=True)
    votes = models.ManyToManyField('Vote', related_name="%(app_label)s_%(class)s_related", blank=True, null=True)

    class Meta:
        abstract = True

    def __unicode__(self):
        return u"%s %s" % (self.law, self.title)

    def get_absolute_url(self):
        if self.bill:
            return self.bill.get_absolute_url()
        return ""

    def get_explanation(self):
        r = re.search(r"דברי הסבר.*?(<p>.*?)<p>-+".decode('utf8'), self.content_html, re.M | re.DOTALL)
        return r.group(1) if r else self.content_html


class PrivateProposal(BillProposal):
    proposal_id = models.IntegerField(blank=True, null=True)
    proposers = models.ManyToManyField('mks.Member', related_name='proposals_proposed', blank=True, null=True)
    joiners = models.ManyToManyField('mks.Member', related_name='proposals_joined', blank=True, null=True)
    bill = models.ForeignKey('Bill', related_name='proposals', blank=True, null=True)


class KnessetProposal(BillProposal):
    committee = models.ForeignKey('committees.Committee', related_name='bills', blank=True, null=True)
    booklet_number = models.IntegerField(blank=True, null=True)
    originals = models.ManyToManyField('PrivateProposal', related_name='knesset_proposals', blank=True, null=True)
    bill = models.OneToOneField('Bill', related_name='knesset_proposal', blank=True, null=True)


class GovProposal(BillProposal):
    booklet_number = models.IntegerField(blank=True, null=True)
    bill = models.OneToOneField('Bill', related_name='gov_proposal', blank=True, null=True)


class BillManager(models.Manager):
    def filter_and_order(self, *args, **kwargs):
        stage = kwargs.get('stage', None)
        member = kwargs.get('member', None)
        pp_id = kwargs.get('pp_id', None)
        knesset_booklet = kwargs.get('knesset_booklet', None)
        gov_booklet = kwargs.get('gov_booklet', None)
        changed_after = kwargs.get('changed_after', None)
        changed_before = kwargs.get('changed_before', None)
        bill_type = kwargs.get('bill_type', 'all')

        filter_kwargs = {}
        if stage and stage != 'all':
            if stage in BILL_AGRR_STAGES:
                qs = self.filter(BILL_AGRR_STAGES[stage])
            else:
                filter_kwargs['stage__startswith'] = stage
                qs = self.filter(**filter_kwargs)
        else:
            qs = self.all()

        if kwargs.get('tagged', None):
            if kwargs['tagged'] == 'false':
                ct = ContentType.objects.get_for_model(Bill)
                filter_tagged = TaggedItem.objects.filter(content_type=ct).distinct().values_list('object_id',
                                                                                                  flat=True)
                qs = qs.exclude(id__in=filter_tagged)
            elif kwargs['tagged'] != 'all':
                qs = TaggedItem.objects.get_by_model(qs, get_tag(kwargs['tagged']))

        if bill_type == 'government':
            qs = qs.exclude(gov_proposal=None)
        elif bill_type == 'knesset':
            qs = qs.exclude(knesset_proposal=None)

        elif bill_type == 'private':
            qs = qs.exclude(proposals=None)

        if pp_id:
            private_proposals = PrivateProposal.objects.filter(
                proposal_id=pp_id).values_list(
                'id', flat=True)
            if private_proposals:
                qs = qs.filter(proposals__in=private_proposals)
            else:
                qs = qs.none()

        if knesset_booklet:
            knesset_proposals = KnessetProposal.objects.filter(
                booklet_number=knesset_booklet).values_list(
                'id', flat=True)
            if knesset_proposals:
                qs = qs.filter(knesset_proposal__in=knesset_proposals)
            else:
                qs = qs.none()
        if gov_booklet:
            government_proposals = GovProposal.objects.filter(
                booklet_number=gov_booklet).values_list('id', flat=True)
            if government_proposals:
                qs = qs.filter(gov_proposal__in=government_proposals)
            else:
                qs = qs.none()

        if changed_after:
            qs = qs.filter(stage_date__gte=changed_after)

        if changed_before:
            qs = qs.filter(stage_date__lte=changed_before)

        return qs


class Bill(models.Model):
    title = models.CharField(max_length=1000)
    full_title = models.CharField(max_length=2000, blank=True)
    slug = models.SlugField(max_length=1000)
    popular_name = models.CharField(max_length=1000, blank=True)
    popular_name_slug = models.CharField(max_length=1000, blank=True)
    law = models.ForeignKey('Law', related_name="bills", blank=True, null=True)
    stage = models.CharField(max_length=10, choices=BILL_STAGE_CHOICES)
    #: date of entry to current stage
    stage_date = models.DateField(blank=True, null=True, db_index=True)
    pre_votes = models.ManyToManyField('Vote', related_name='bills_pre_votes', blank=True,
                                       null=True)  # link to pre-votes related to this bill
    first_committee_meetings = models.ManyToManyField('committees.CommitteeMeeting', related_name='bills_first',
                                                      blank=True,
                                                      null=True)  # CM related to this bill, *before* first vote
    first_vote = models.ForeignKey('Vote', related_name='bills_first', blank=True, null=True)  # first vote of this bill
    second_committee_meetings = models.ManyToManyField('committees.CommitteeMeeting', related_name='bills_second',
                                                       blank=True,
                                                       null=True)  # CM related to this bill, *after* first vote
    approval_vote = models.OneToOneField('Vote', related_name='bill_approved', blank=True,
                                         null=True)  # approval vote of this bill
    proposers = models.ManyToManyField('mks.Member', related_name='bills', blank=True,
                                       null=True)  # superset of all proposers of all private proposals related to this bill
    joiners = models.ManyToManyField('mks.Member', related_name='bills_joined', blank=True,
                                     null=True)  # superset of all joiners

    objects = BillManager()

    class Meta:
        ordering = ('-stage_date', '-id')
        verbose_name = _('Bill')
        verbose_name_plural = _('Bills')

    def __unicode__(self):
        return u"%s %s (%s)" % (self.law, self.title, self.get_stage_display())

    @models.permalink
    def get_absolute_url(self):
        return ('bill-detail', [str(self.id)])

    def save(self, **kwargs):
        self.slug = slugify_name(self.title)
        self.popular_name_slug = slugify_name(self.popular_name)
        if self.law:
            self.full_title = "%s %s" % (self.law.title, self.title)
        else:
            self.full_title = self.title
        super(Bill, self).save(**kwargs)
        for mk in self.proposers.all():
            mk.recalc_bill_statistics()

    def _get_tags(self):
        tags = Tag.objects.get_for_object(self)
        return tags

    def _set_tags(self, tag_list):
        Tag.objects.update_tags(self, tag_list)

    tags = property(_get_tags, _set_tags)

    def merge(self, another_bill):
        """Merges another_bill into self, and delete another_bill"""
        if not self.id:
            logger.debug('trying to merge into a bill with id=None, title=%s',
                         self.title)
            self.save()
        if not another_bill.id:
            logger.debug('trying to merge a bill with id=None, title=%s',
                         another_bill.title)
            another_bill.save()

        if self is another_bill:
            logger.debug('abort merging bill %d into itself' % self.id)
            return
        logger.debug('merging bill %d into bill %d' % (another_bill.id,
                                                       self.id))

        other_kp = KnessetProposal.objects.filter(bill=another_bill)
        my_kp = KnessetProposal.objects.filter(bill=self)
        if my_kp and other_kp:
            logger.debug('abort merging bill %d into bill %d, because both '
                         'have KPs' % (another_bill.id, self.id))
            return

        for pv in another_bill.pre_votes.all():
            self.pre_votes.add(pv)
        for cm in another_bill.first_committee_meetings.all():
            self.first_committee_meetings.add(cm)
        if not self.first_vote and another_bill.first_vote:
            self.first_vote = another_bill.first_vote
        for cm in another_bill.second_committee_meetings.all():
            self.second_committee_meetings.add(cm)
        if not self.approval_vote and another_bill.approval_vote:
            self.approval_vote = another_bill.approval_vote
        for m in another_bill.proposers.all():
            self.proposers.add(m)
        for pp in another_bill.proposals.all():
            pp.bill = self
            pp.save()
        if other_kp:
            other_kp[0].bill = self
            other_kp[0].save()

        bill_ct = ContentType.objects.get_for_model(self)
        Comment.objects.filter(content_type=bill_ct,
                               object_pk=another_bill.id).update(
            object_pk=self.id)
        for v in voting.models.Vote.objects.filter(content_type=bill_ct,
                                                   object_id=another_bill.id):
            if voting.models.Vote.objects.filter(content_type=bill_ct,
                                                 object_id=self.id,
                                                 user=v.user).count() == 0:
                # only if this user did not vote on self, copy the vote from
                # another_bill
                v.object_id = self.id
                v.save()
        for f in Follow.objects.filter(content_type=bill_ct,
                                       object_id=another_bill.id):
            try:
                f.object_id = self.id
                f.save()
            except IntegrityError:  # self was already being followed by the
                # same user
                pass
        for ti in TaggedItem.objects.filter(content_type=bill_ct,
                                            object_id=another_bill.id):
            if ti.tag not in self.tags:
                ti.object_id = self.id
                ti.save()
        for ab in another_bill.agendabills.all():
            try:
                ab.bill = self
                ab.save()
            except IntegrityError:  # self was already in this agenda
                pass
        for be in another_bill.budget_ests.all():
            try:
                be.bill = self
                be.save()
            except IntegrityError:  # same user already estimated self
                pass
        another_bill.delete()
        self.update_stage()

    def update_votes(self):
        used_votes = []  # ids of votes already assigned 'roles', so we won't match a vote in 2 places
        gp = GovProposal.objects.filter(bill=self)
        if gp:
            gp = gp[0]
            for this_v in gp.votes.all():
                if this_v.title.find('אישור'.decode('utf8')) == 0:
                    self.approval_vote = this_v
                    used_votes.append(this_v.id)
                if this_v.title.find('להעביר את'.decode('utf8')) == 0:
                    self.first_vote = this_v

        kp = KnessetProposal.objects.filter(bill=self)
        if kp:
            for this_v in kp[0].votes.all():
                if this_v.title.find('אישור'.decode('utf8')) == 0:
                    self.approval_vote = this_v
                    used_votes.append(this_v.id)
                if this_v.title.find('להעביר את'.decode('utf8')) == 0:
                    if this_v.time.date() > kp[0].date:
                        self.first_vote = this_v
                    else:
                        self.pre_votes.add(this_v)
                    used_votes.append(this_v.id)
        pps = PrivateProposal.objects.filter(bill=self)
        if pps:
            for pp in pps:
                for this_v in pp.votes.all():
                    if this_v.id not in used_votes:
                        self.pre_votes.add(this_v)
        self.update_stage()

    def update_stage(self, force_update=False):
        """
        Updates the stage for this bill according to all current data
        force_update - assume current stage is wrong, and force
        recalculation. default is False, so we assume current status is OK,
        and only look for updates.
        """
        if not self.stage_date or force_update:  # might be empty if bill is new
            self.stage_date = FIRST_KNESSET_START
        if self.approval_vote:
            if self.approval_vote.for_votes_count > self.approval_vote.against_votes_count:
                self.stage = BillStages.APPROVED
            else:
                self.stage = BillStages.FAILED_APPROVAL
            self.stage_date = self.approval_vote.time.date()
            self.save()
            return
        for cm in self.second_committee_meetings.all():
            if not (self.stage_date) or self.stage_date < cm.date:
                self.stage = BillStages.COMMITTEE_CORRECTIONS
                self.stage_date = cm.date
        if self.stage == BillStages.COMMITTEE_CORRECTIONS:
            self.save()
            return
        if self.first_vote:
            if self.first_vote.for_votes_count > self.first_vote.against_votes_count:
                self.stage = BillStages.FIRST_VOTE
            else:
                self.stage = BillStages.FAILED_FIRST_VOTE
            self.stage_date = self.first_vote.time.date()
            self.save()
            return
        try:
            kp = self.knesset_proposal
            if not (self.stage_date) or self.stage_date < kp.date:
                self.stage = BillStages.IN_COMMITTEE
                self.stage_date = kp.date
        except KnessetProposal.DoesNotExist:
            pass
        try:
            gp = self.gov_proposal
            if not (self.stage_date) or self.stage_date < gp.date:
                self.stage = BillStages.IN_COMMITTEE
                self.stage_date = gp.date
        except GovProposal.DoesNotExist:
            pass
        for cm in self.first_committee_meetings.all():
            if not (self.stage_date) or self.stage_date < cm.date:
                # if it was converted to discussion, seeing it in
                # a cm doesn't mean much.
                if self.stage != BillStages.CONVERTED_TO_DISCUSSION:
                    self.stage = BillStages.IN_COMMITTEE
                    self.stage_date = cm.date
        for v in self.pre_votes.all():
            if not (self.stage_date) or self.stage_date < v.time.date():
                for h in CONVERT_TO_DISCUSSION_HEADERS:
                    if v.title.find(h) >= 0:
                        self.stage = BillStages.CONVERTED_TO_DISCUSSION  # converted to discussion
                        self.stage_date = v.time.date()
        for v in self.pre_votes.all():
            if not (self.stage_date) or self.stage_date < v.time.date():
                if v.for_votes_count > v.against_votes_count:
                    self.stage = BillStages.PRE_APPROVED
                else:
                    self.stage = BillStages.FAILED_PRE_APPROVAL
                self.stage_date = v.time.date()
        for pp in self.proposals.all():
            if not (self.stage_date) or self.stage_date < pp.date:
                self.stage = BillStages.PROPOSED
                self.stage_date = pp.date
        self.save()
        self.generate_activity_stream()

    def generate_activity_stream(self):
        ''' create an activity stream based on the data stored in self '''

        Action.objects.stream_for_actor(self).delete()
        ps = list(self.proposals.all())
        try:
            ps.append(self.gov_proposal)
        except GovProposal.DoesNotExist:
            pass

        for p in ps:
            action.send(self, verb='was-proposed', target=p,
                        timestamp=p.date, description=p.title)

        try:
            p = self.knesset_proposal
            action.send(self, verb='was-knesset-proposed', target=p,
                        timestamp=p.date, description=p.title)
        except KnessetProposal.DoesNotExist:
            pass

        for v in self.pre_votes.all():
            discussion = False
            for h in CONVERT_TO_DISCUSSION_HEADERS:
                if v.title.find(h) >= 0:  # converted to discussion
                    discussion = True
            if discussion:
                action.send(self, verb='was-converted-to-discussion', target=v,
                            timestamp=v.time)
            else:
                action.send(self, verb='was-pre-voted', target=v,
                            timestamp=v.time, description=v.passed)

        if self.first_vote:
            action.send(self, verb='was-first-voted', target=self.first_vote,
                        timestamp=self.first_vote.time, description=self.first_vote.passed)

        if self.approval_vote:
            action.send(self, verb='was-approval-voted', target=self.approval_vote,
                        timestamp=self.approval_vote.time, description=self.approval_vote.passed)

        for cm in self.first_committee_meetings.all():
            action.send(self, verb='was-discussed-1', target=cm,
                        timestamp=cm.date, description=cm.committee.name)

        for cm in self.second_committee_meetings.all():
            action.send(self, verb='was-discussed-2', target=cm,
                        timestamp=cm.date, description=cm.committee.name)

        for g in self.gov_decisions.all():
            action.send(self, verb='was-voted-on-gov', target=g,
                        timestamp=g.date, description=str(g.stand))

    @property
    def frozen(self):
        return self.stage == u'0'

    @property
    def latest_private_proposal(self):
        return self.proposals.order_by('-date').first()

    @property
    def stage_id(self):
        try:
            return (key for key, value in BILL_STAGES.items() if value == self.stage).next()
        except StopIteration:
            return BILL_STAGES['UNKNOWN']

    def is_past_stage(self, lookup_stage_id):
        res = False
        my_stage_id = self.stage_id
        for iter_stage_id in BILL_STAGES.keys():
            if lookup_stage_id == iter_stage_id:
                res = True
            if my_stage_id == iter_stage_id:
                break
        return res


def add_tags_to_bill_related_objects(sender, instance, **kwargs):
    bill_ct = ContentType.objects.get_for_model(instance)
    for ti in TaggedItem.objects.filter(content_type=bill_ct, object_id=instance.id):
        add_tags_to_related_objects(sender, ti, **kwargs)


post_save.connect(add_tags_to_bill_related_objects, sender=Bill)


def get_n_debated_bills(n=None):
    """Returns n random bills that have an active debate in the site.
    if n is None, it returns all of them."""

    bill_votes = [x['object_id'] for x in voting.models.Vote.objects.get_popular(Bill)]
    if not bill_votes:
        return None

    bills = Bill.objects.filter(pk__in=bill_votes,
                                stage_date__gt=Knesset.objects.current_knesset().start_date)
    if (n is not None) and (n < len(bill_votes)):
        bills = random.sample(bills, n)
    return bills


def get_debated_bills():
    """
    Returns 3 random bills that have an active debate in the site
    """
    debated_bills = cache.get('debated_bills')
    if not debated_bills:
        debated_bills = get_n_debated_bills(3)
        cache.set('debated_bills', debated_bills, settings.LONG_CACHE_TIME)
    return debated_bills


class GovLegislationCommitteeDecision(models.Model):
    title = models.CharField(max_length=1000)
    subtitle = models.TextField(null=True, blank=True)
    text = models.TextField(blank=True, null=True)
    date = models.DateField(blank=True, null=True)
    source_url = models.URLField(max_length=1024, null=True, blank=True)
    bill = models.ForeignKey('Bill', blank=True, null=True, related_name='gov_decisions')
    stand = models.IntegerField(blank=True, null=True)
    number = models.IntegerField(blank=True, null=True)

    def __unicode__(self):
        return u"%s" % (self.title)

    def get_absolute_url(self):
        return self.bill.get_absolute_url()


class BillBudgetEstimation(models.Model):
    class Meta:
        unique_together = (("bill", "estimator"),)

    bill = models.ForeignKey("Bill", related_name="budget_ests")
    # costs are in thousands NIS
    one_time_gov = models.IntegerField(blank=True, null=True)
    yearly_gov = models.IntegerField(blank=True, null=True)
    one_time_ext = models.IntegerField(blank=True, null=True)
    yearly_ext = models.IntegerField(blank=True, null=True)
    estimator = models.ForeignKey(User, related_name="budget_ests", blank=True, null=True)
    time = models.DateTimeField(auto_now=True)
    summary = models.TextField(null=True, blank=True)

    def as_p(self):
        return mark_safe(("<p><label><b>%s</b></label> %s</p>\n" * 7) % \
                         (
                             # leave this; the lazy translator does not evaluate for some reason.
                             _('Estimation of').format(),
                             "<b>%s</b>" % self.estimator.username,
                             _('Estimated on:').format(),
                             self.time,
                             _('One-time costs to government:').format(),
                             get_thousands_string(self.one_time_gov),
                             _('Yearly costs to government:').format(),
                             get_thousands_string(self.yearly_gov),
                             _('One-time costs to external bodies:').format(),
                             get_thousands_string(self.one_time_ext),
                             _('Yearly costs to external bodies:').format(),
                             get_thousands_string(self.yearly_ext),
                             _('Summary of the estimation:').format(),
                             escape(self.summary if self.summary else "", )))


def get_thousands_string(f):
    """
    Get a nice string representation of a field of 1000's of NIS, which is int or None.
    """
    if f is None:
        return "N/A"
    elif f == 0:
        return "0 NIS"
    else:
        return "%d000 NIS" % f


from listeners import *
