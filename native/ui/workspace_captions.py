"""Caption review subordinate to the active editor binding and its one action token."""
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,QDialog,QHBoxLayout,QLabel,QPlainTextEdit,QPushButton,QScrollArea,
    QTabWidget,QToolButton,QVBoxLayout,QWidget,QMessageBox,
)
from native.ui.caption_timing import TimingRepairPanel,TimingRepairBinding,source_seconds
from native.ui import theme


def label(text=''):
    value=QLabel(text);value.setWordWrap(True);value.setTextFormat(Qt.TextFormat.PlainText)
    return value


class CaptionDialog(QDialog):
    def __init__(self,host):
        super().__init__(host.window)
        self.setObjectName('stage');self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        self.setStyleSheet(theme.GLOBAL_QSS+f'''
            QComboBox, QToolButton, QTabBar::tab {{ background: {theme.BG_PANEL}; color: {theme.INK}; border: 1px solid {theme.BORDER}; padding: 6px; }}
            QComboBox:disabled {{ color: {theme.INK_MUTED}; }}
            QTabBar::tab:selected {{ color: {theme.INK_BRIGHT}; border-bottom: 2px solid {theme.ACCENT}; }}
            QTabWidget::pane {{ border: 1px solid {theme.BORDER}; }}
        ''')
        self.host=host;self.outcome=None;self.track_boxes=[]
        self.setWindowTitle('Moment captions');self.resize(760,600)
        body=QVBoxLayout(self);body.setSpacing(4);body.setContentsMargins(10,8,10,8)
        self.title=label('Captions');self.title.setProperty('title',True);body.addWidget(self.title)
        self.range=label();body.addWidget(self.range)
        self.status=label('Loading the active Moment…');body.addWidget(self.status)
        self.tabs=QTabWidget();body.addWidget(self.tabs,1)
        review=QWidget();layout=QVBoxLayout(review);layout.setSpacing(4)
        self.profile=label();layout.addWidget(self.profile)
        self.audio=QComboBox();self.audio.setAccessibleName('Actual source audio stream and channels');layout.addWidget(self.audio)
        self.tracks=QWidget();self.tracks_layout=QVBoxLayout(self.tracks);self.tracks_layout.setContentsMargins(0,0,0,0);layout.addWidget(self.tracks)
        self.text=QPlainTextEdit();self.text.setReadOnly(True);self.text.setPlaceholderText('Generate locally to review a proposal. Accepted words change only after your explicit choice.')
        self.text.setAccessibleName('Generated proposal text');layout.addWidget(self.text,1)
        self.evidence=label();layout.addWidget(self.evidence)
        row=QHBoxLayout()
        self.reload_context=QPushButton('Reload context');self.reload_context.clicked.connect(self._reload_context);row.addWidget(self.reload_context)
        self.generate=QPushButton('Generate locally');self.generate.clicked.connect(self._generate);row.addWidget(self.generate)
        self.accept=QPushButton('Accept these words');self.accept.clicked.connect(self._accept);row.addWidget(self.accept)
        self.cancel=QPushButton('Cancel generation');self.cancel.clicked.connect(host._cancel_open);row.addWidget(self.cancel)
        layout.addLayout(row)
        detailrow=QHBoxLayout()
        self.details_toggle=QToolButton();self.details_toggle.setText('Details');self.details_toggle.setCheckable(True);detailrow.addWidget(self.details_toggle)
        self.observe=QPushButton('Check caption process');self.observe.clicked.connect(lambda:self._observe(False));detailrow.addWidget(self.observe)
        self.cleanup=QPushButton('Stop and settle caption process');self.cleanup.clicked.connect(lambda:self._observe(True));detailrow.addWidget(self.cleanup)
        layout.addLayout(detailrow)
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setMaximumHeight(100);self.details.hide();layout.addWidget(self.details)
        self.details_toggle.toggled.connect(self.details.setVisible)
        self.tabs.addTab(review,'Proposal')
        self.repair=TimingRepairPanel();self.repair.setMinimumSize(0,0)
        self.repair.layout().setContentsMargins(8,8,8,8)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(self.repair)
        self.tabs.addTab(scroll,'Accepted words')
        self.repair_binding=TimingRepairBinding(self.repair,RepairProxy(self),host.runner,host_dispatch=self._repair_dispatch)
        monitor=QHBoxLayout()
        self.monitor_buttons=[]
        for text,action in [('Show in Source','show'),('Play source','play'),('Stop source','stop')]:
            button=QPushButton(text);button.clicked.connect(lambda checked=False,a=action:self._source(a))
            monitor.addWidget(button);self.monitor_buttons.append(button)
        body.addLayout(monitor)
        for field in (self.repair.spelling,self.repair.start_time,self.repair.end_time):
            field.textChanged.connect(self.controls)

    def _repair_dispatch(self,fn,*args,on_finished,on_error):
        def finished(result):
            self.repair.set_busy(False);on_finished(result);self.refresh(self.host.controller.caption_status(),preserve_draft=True)
        def failed(error):
            self.repair.set_busy(False);on_error(error);self.refresh(self.host.controller.caption_status(),preserve_draft=True)
        self.host.run_caption(lambda:fn(*args),'Updating the saved captions…',on_finished=finished,on_error=failed,
                              on_started=lambda:self.repair.set_busy(True))

    def refresh(self,outcome,*,preserve_draft=False):
        if outcome is None:return
        previous=self.outcome;self.outcome=outcome
        self.title.setText(outcome.title or 'Moment captions')
        text=''
        if outcome.source_range:
            text=f'Moment: {source_seconds(outcome.source_range[0])}–{source_seconds(outcome.source_range[1])} source seconds'
        if outcome.analysis_range and outcome.analysis_range!=outcome.source_range:
            text+=f' · Analysis: {source_seconds(outcome.analysis_range[0])}–{source_seconds(outcome.analysis_range[1])}'
        self.range.setText(text);self.status.setText(outcome.message)
        self.profile.setText(outcome.profile_name+' · Local evaluation profile; words and timing need your review.')
        if previous is None or previous.context_token!=outcome.context_token:
            self.audio.clear()
            for stream in outcome.streams:
                self.audio.addItem(f"Source audio · global stream {stream['stream_index']} · all {len(stream['channels'])} channels",stream)
            while self.tracks_layout.count():
                item=self.tracks_layout.takeAt(0)
                if item.widget():item.widget().deleteLater()
            self.track_boxes=[]
            for group in outcome.track_choices:
                box=QComboBox();box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon);box.setMinimumContentsLength(20)
                box.setAccessibleName(group['role']+' caption track')
                box.addItem('Choose '+group['role']+' caption track…',None)
                for track in group['tracks']:
                    name=f"{group['role'].capitalize()} · {track['label']} · {track.get('name') or 'Caption track'} · {track.get('language') or 'Language unset'}"
                    box.addItem(name,{'sequence':group['sequence'],'track':track['id']})
                    box.setItemData(box.count()-1,json.dumps(track,ensure_ascii=False),Qt.ItemDataRole.ToolTipRole)
                chosen=[t for t in outcome.tracks if t['sequence']==group['sequence']]
                for index in range(1,box.count()):
                    if chosen and box.itemData(index)['track']==chosen[0]['track']:box.setCurrentIndex(index)
                self.track_boxes.append(box);self.tracks_layout.addWidget(box)
                box.currentIndexChanged.connect(self.controls)
        proposal=outcome.proposal
        self.text.setPlainText(proposal['text'] if proposal else '')
        self.evidence.setText(f"{proposal['word_count']} proposed words · {proposal['unresolved']} words need timing review. Generation does not accept or apply words." if proposal else 'Spelling, timing, and listening quality need review. No quality score is inferred.')
        self.details.setPlainText(json.dumps({'proposal':proposal,'tracks':outcome.track_choices},ensure_ascii=False,indent=2))
        if outcome.repair is not None and not preserve_draft:
            self.repair.set_view(outcome.repair)
        self.tabs.setTabEnabled(1,outcome.repair is not None)
        if outcome.repair is not None and outcome.state!='proposal' and not preserve_draft:self.tabs.setCurrentIndex(1)
        self.controls()

    def controls(self):
        out=self.outcome;busy=self.host.busy
        word=self.repair.selected_word()
        dirty=bool(word and (self.repair.spelling.text()!=word.text or
            self.repair.start_time.text()!=source_seconds(word.start_us) or self.repair.end_time.text()!=source_seconds(word.end_us)))
        ready=bool(out and not busy and not dirty and all(b.currentData() is not None for b in self.track_boxes) and len(self.track_boxes)==3)
        self.generate.setEnabled(ready and out.can_generate and self.audio.currentData() is not None)
        self.accept.setEnabled(ready and out.can_accept)
        self.cancel.setVisible(self.host.cancel is not None);self.cancel.setEnabled(self.host.cancel is not None and not self.host.cancel.is_set())
        self.observe.setVisible(bool(out and out.can_cleanup));self.cleanup.setVisible(bool(out and out.can_cleanup))
        self.observe.setEnabled(not busy);self.cleanup.setEnabled(not busy)
        self.reload_context.setEnabled(not busy and not bool(out and out.requires_reconciliation and out.can_cleanup))
        self.audio.setEnabled(not busy)
        for button in self.monitor_buttons:button.setEnabled(bool(out and out.context_token and not busy and not out.requires_reconciliation))
        for box in self.track_boxes:box.setEnabled(not busy and not bool(out and out.repair))
        self.repair.set_busy(busy)
        self.repair.table.setEnabled(not busy and not dirty)

    def _reload_context(self):
        word=self.repair.selected_word()
        dirty=word is not None and (self.repair.spelling.text()!=word.text
            or self.repair.start_time.text()!=source_seconds(word.start_us)
            or self.repair.end_time.text()!=source_seconds(word.end_us))
        if dirty and QMessageBox.question(self,'Discard unsaved draft?',
            'Reloading caption context discards the unsaved spelling or timing draft. Continue?',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Yes:return
        current=self.host.controller.status()
        self.host.run_caption(lambda:self.host.controller.caption_context(current.moment_id,current.moment_revision,cancel=self.host.cancel),
            'Reloading the active caption context…',on_finished=self.refresh,cancellable=True)

    def _generate(self):
        stream=self.audio.currentData()
        selection={'stream_index':stream['stream_index'],'channels':stream['channels'],
                   'tracks':[b.currentData() for b in self.track_boxes]}
        self.host.run_caption(lambda:self.host.controller.generate_captions(self.outcome.context_token,selection,cancel=self.host.cancel),
                              'Generating a local caption proposal…',on_finished=self.refresh,cancellable=True)

    def _accept(self):
        p=self.outcome.proposal
        self.host.run_caption(lambda:self.host.controller.accept_caption_proposal(self.outcome.context_token,
            p['job_id'],p['transcript_id'],p['hypothesis_id'],p['alignment_id'],expected_revision=0,tracks=[b.currentData() for b in self.track_boxes]),
            'Accepting the selected words…',on_finished=self.refresh)

    def _observe(self,cleanup):
        self.host.run_caption(lambda:self.host.controller.observe_captions(cleanup=cleanup),
                              'Checking the original caption owner…',on_finished=self.refresh)

    def _source(self,action):
        word=self.repair.selected_word() if self.tabs.currentIndex()==1 else None
        self.host.run_caption(lambda:self.host.controller.caption_source_monitor(self.outcome.context_token,action,
            word_id=word.id if word is not None and action=='show' else None),
            'Updating Filmcraft Source monitor…',on_finished=lambda out:self.refresh(out,preserve_draft=True))


class RepairProxy:
    """Closed actions only; the host dispatches each through the editor's actual lock."""
    def __init__(self,dialog):self.dialog=dialog
    def _call(self,action,view=None,**fields):
        d=self.dialog
        result=d.host.controller.repair_captions(d.outcome.context_token,action,view,**fields)
        if result.repair is None or result.state=='blocked':raise ValueError(result.message)
        return result.repair
    def load(self,transcript_id,revision):return self._call('load',transcript_id=transcript_id,revision=revision)
    def reload_saved(self,transcript_id):return self._call('reload',transcript_id=transcript_id)
    def save_spelling(self,view,word_id,text):return self._call('replace',view,word_id=word_id,text=text)
    def save_timing(self,view,word_id,start_us,end_us):return self._call('retime',view,word_id=word_id,start_us=start_us,end_us=end_us)
    def apply_saved(self,view):return self._call('apply',view)
    def check_editor(self,view):return self._call('check',view)
