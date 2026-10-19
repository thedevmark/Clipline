"""Approved authored titles through the existing native editor owner."""
from dataclasses import dataclass
import json
import uuid
from native.services.workspace_editor import OperationRefused
from native.services.filmcraft_bridge import NativeCommandError

KEY = 'clipline.creativeTitles.v1'
PATTERNS = {
    'hook': (
        ('none','Use the footage alone','','No added hook title.'),
        ('H04','Mistake and consequence','i picked the worst time to reload','The reload and its consequence are visible or audible.'),
        ('H03','Celebration reversal','i celebrated one second too early','The real celebration precedes the setback.'),
        ('H09','Discovery','i did not know this could happen','The footage contains the surprising event.'),
        ('manual','My own hook','','These words describe this footage accurately.'),
    ),
    'cta': (
        ('none','No CTA','','Leave the ending clean.'),
        ('question','One relevant question','what would you have tried here','The payoff is complete and this question fits the visible decision.'),
        ('stream','Invite viewers to the stream','come make the next bad decision with chat','This channel link is current and this invitation fits the ending.'),
        ('series','Continue a real series','follow for the next attempt','Another attempt is actually planned and the payoff remains clear.'),
    ),
}


@dataclass(frozen=True)
class TitleDraft:
    kind: str
    recipe: str
    text: str
    evidence_confirmed: bool
    duration: float = 2.0
    placement: str = 'top'
    destination: str = ''

    def command_fields(self):
        rows={row[0]:row for row in PATTERNS.get(self.kind,())}
        if self.recipe not in rows or self.recipe=='none':
            raise ValueError('Choose a title pattern, or leave the footage without added text.')
        if not self.evidence_confirmed:
            raise ValueError('Confirm that this wording matches the footage before adding it.')
        if (not isinstance(self.text,str) or not self.text.strip() or len(self.text)>160
                or any(ord(c)<32 or ord(c)==127 for c in self.text)):
            raise ValueError('Use one short plain-text title, up to 160 characters.')
        if type(self.duration) not in (int,float) or not .25<=self.duration<=5 or self.placement not in ('top','bottom'):
            raise ValueError('Choose 0.25–5 seconds and a top or bottom placement.')
        destination=self.destination.strip()
        if self.recipe=='stream' and not destination:
            raise ValueError('Add the current channel link for the stream invitation.')
        if destination and (self.kind!='cta' or not destination.startswith('https://')
                or len(destination)>2048 or any(c.isspace() or ord(c)<32 for c in destination)):
            raise ValueError('Use an explicit HTTPS destination for this CTA.')
        return {'kind':self.kind,'recipe':self.recipe,'text':self.text,'evidence':rows[self.recipe][3],
                'durationSeconds':self.duration,'placement':self.placement,'destination':destination or None}


@dataclass(frozen=True)
class CreativeOutcome:
    state: str
    message: str
    token: str | None = None
    title: str = ''
    compositions: tuple = ()
    applied: object | None = None


class WorkspaceCreative:
    """No process, timeline or publication owner; the editor serializes these calls."""
    def __init__(self,editor):
        self.editor=editor
        self._context=None

    def _identity(self):
        e=self.editor;f=e._facts
        return (id(e._session),f.moment.id,f.moment.revision,f.kit.descriptor_sha256,
                f.source.sha256,str(e._view.working.path))

    def _admit(self):
        e=self.editor
        if not e._document_is_open():raise OperationRefused('Open a settled Moment edit before adding a title.')
        e._assert_facts()
        return e

    def context(self):
        values=[]
        def operation():
            e=self._admit();revision,links=e._inspect();bridge=e._session.bridge
            inventory=bridge.execute('clipline.validate',{'expectedRevision':revision,'expectedLinksRevisions':links})
            if len(inventory['owners'])!=1:raise OperationRefused('Choose a Moment with one linked Creator Kit owner.')
            owner=inventory['owners'][0]
            metadata=bridge.execute('metadata.get',{'item':owner['owner']})['metadata']
            raw=metadata.get(KEY,'[]')
            if not isinstance(raw,str) or len(raw)>65536:raise OperationRefused('The title inventory cannot be read safely.')
            records=json.loads(raw)
            if not isinstance(records,list) or len(records)>6:raise OperationRefused('The title inventory is unsupported.')
            compositions=[]
            for role in ('landscape','portrait'):
                seq=owner['sequences'][role];info=bridge.execute('sequence.inspect',{'item':seq})
                clips={item['clip'] for track in info['video'] for item in track['items']}
                compositions.append({'role':role,'sequence':seq,'width':info['settings']['width'],'height':info['settings']['height'],
                    'existing':tuple(r['kind'] for r in records if r.get('sequence')==seq and r.get('clip') in clips)})
            if e._inspect()!=(revision,links):raise OperationRefused('The editor changed. Reload the title context.')
            token=str(uuid.uuid4())
            self._context={'token':token,'identity':self._identity(),'revision':revision,'links':links,
                'owner':owner['owner'],'compositions':compositions}
            values.append(CreativeOutcome('ready','Choose the wording and the one composition that should receive it.',
                token,e._facts.moment.title,tuple(compositions)))
        outcome=self.editor._run(operation)
        return values[0] if values else CreativeOutcome('blocked',outcome.message)

    def apply(self,token,composition,draft):
        fields=draft.command_fields()
        values=[]
        def operation():
            e=self._admit();context=self._context
            if context is None or token!=context['token'] or context['identity']!=self._identity():
                raise OperationRefused('Reload this exact Moment before adding a title.')
            choices={row['role']:row for row in context['compositions']}
            if composition not in choices:raise OperationRefused('Choose the landscape or portrait composition.')
            if e._inspect()!=(context['revision'],context['links']):
                raise OperationRefused('The editor changed. Reload context; your wording remains in the form.')
            sequence=choices[composition]['sequence']
            params={**fields,'expectedProjectPath':str(e._view.working.path),'expectedRevision':context['revision'],
                'expectedLinksRevisions':context['links'],'owner':context['owner'],'sequence':sequence}
            try:
                result=e._session.bridge.execute('clipline.addCreativeTitle',params)
            except NativeCommandError as error:
                # The qualified command owns rollback; a received refusal is safe to display.
                values.append(CreativeOutcome('blocked',error.reason,token,e._facts.moment.title,tuple(context['compositions'])))
                return
            if (not isinstance(result,dict) or result.get('owner')!=context['owner'] or result.get('sequence')!=sequence
                    or result.get('text')!=fields['text'] or type(result.get('clip')) is not int):
                raise ValueError('The native title receipt differs. Check the editor before any retry.')
            self._context=None
            values.append(CreativeOutcome('applied',f'Title added to {composition} only. Edit its graphic in Filmcraft, then save a checkpoint.',
                title=e._facts.moment.title,applied=result))
        outcome=self.editor._run(operation)
        return values[0] if values else CreativeOutcome('blocked',outcome.message)
