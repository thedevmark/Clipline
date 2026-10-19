"""A small authored-title dialog subordinate to the existing editor binding."""
from PySide6.QtCore import Qt, QObject
from PySide6.QtWidgets import (
    QCheckBox,QComboBox,QDialog,QDoubleSpinBox,QFormLayout,QHBoxLayout,QLabel,QLineEdit,
    QPushButton,QVBoxLayout,
)
from native.services.workspace_creative import PATTERNS,TitleDraft,WorkspaceCreative
from native.ui import theme

def label(text):
    widget=QLabel(text);widget.setWordWrap(True);widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


class CreativeDialog(QDialog):
    def __init__(self,binding):
        super().__init__(binding.host.window)
        self.binding=binding;self.context=None
        self.setWindowTitle('Hook / CTA');self.resize(620,520)
        self.setObjectName('stage');self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        self.setStyleSheet(theme.GLOBAL_QSS+f'''
            QComboBox, QDoubleSpinBox {{
                background: {theme.BG_PANEL}; color: {theme.INK_BRIGHT};
                border: 1px solid {theme.BORDER}; border-radius: 6px;
                padding: 7px 10px;
            }}
            QComboBox QAbstractItemView {{
                background: {theme.BG_PANEL}; color: {theme.INK_BRIGHT};
                selection-background-color: {theme.BG_HOVER};
            }}
        ''')
        body=QVBoxLayout(self)
        self.title=label('Hook / CTA');self.title.setProperty('title',True);body.addWidget(self.title)
        body.addWidget(label('Add one editable native graphic. The other format keeps its current edit.'))
        self.status=label('Loading the active Moment…');body.addWidget(self.status)
        form=QFormLayout();body.addLayout(form)
        self.kind=QComboBox();self.kind.addItem('Opening hook','hook');self.kind.addItem('Ending CTA','cta')
        self.kind.currentIndexChanged.connect(self._patterns);form.addRow('Title type',self.kind)
        self.pattern=QComboBox();self.pattern.currentIndexChanged.connect(self._pattern);form.addRow('Pattern',self.pattern)
        self.text=QLineEdit();self.text.setMaxLength(160);self.text.setAccessibleName('Approved authored title');form.addRow('Wording',self.text)
        self.composition=QComboBox();form.addRow('Composition',self.composition)
        self.duration=QDoubleSpinBox();self.duration.setRange(.25,5);self.duration.setSingleStep(.25);self.duration.setValue(2);self.duration.setSuffix(' seconds');form.addRow('Duration',self.duration)
        self.position=QComboBox();self.position.addItem('Top','top');self.position.addItem('Bottom','bottom');form.addRow('Placement',self.position)
        self.destination=QLineEdit();self.destination.setMaxLength(2048);self.destination.setPlaceholderText('https://twitch.tv/your-channel');form.addRow('Destination',self.destination)
        self.evidence=label('');body.addWidget(self.evidence)
        self.confirm=QCheckBox('This matches the footage');body.addWidget(self.confirm)
        self.timing=label('The hook overlays the opening. A CTA overlays the ending; keep the payoff clear.');body.addWidget(self.timing)
        body.addStretch()
        row=QHBoxLayout();self.reload=QPushButton('Reload context');self.reload.clicked.connect(binding.load);row.addWidget(self.reload)
        self.add=QPushButton('Add title');self.add.setProperty('primary',True);self.add.clicked.connect(self._apply);row.addWidget(self.add)
        close=QPushButton('Close');close.clicked.connect(self.close);row.addWidget(close);body.addLayout(row)
        for signal in (self.text.textChanged,self.confirm.toggled,self.destination.textChanged,self.composition.currentIndexChanged):signal.connect(self.controls)
        self._patterns()

    def _patterns(self):
        self.pattern.blockSignals(True);self.pattern.clear()
        for identity,name,text,evidence in PATTERNS[self.kind.currentData()]:self.pattern.addItem(name,(identity,text,evidence))
        self.pattern.blockSignals(False);self._pattern()

    def _pattern(self):
        row=self.pattern.currentData()
        if row is None:return
        self.text.setText(row[1]);self.evidence.setText(row[2]);self.confirm.setChecked(False)
        self.destination.setEnabled(self.kind.currentData()=='cta')
        if self.kind.currentData()!='cta':self.destination.clear()
        self.controls()

    def draft(self):
        return TitleDraft(self.kind.currentData(),self.pattern.currentData()[0],self.text.text(),self.confirm.isChecked(),
            self.duration.value(),self.position.currentData(),self.destination.text())

    def controls(self):
        ready=not self.binding.host.busy and self.context is not None and self.context.token is not None
        try:self.draft().command_fields();valid=True
        except (ValueError,TypeError):valid=False
        current=self.composition.currentData()
        existing=() if current is None else current.get('existing',())
        self.add.setEnabled(ready and valid and self.kind.currentData() not in existing)
        self.reload.setEnabled(not self.binding.host.busy)

    def refresh(self,outcome):
        self.status.setText(outcome.message)
        if outcome.title:self.title.setText(outcome.title)
        if outcome.state=='ready':
            self.context=outcome;previous=self.composition.currentData();self.composition.clear()
            for row in outcome.compositions:
                self.composition.addItem(f"{row['role'].title()} · {row['width']} × {row['height']}",row)
            if previous:
                for index in range(self.composition.count()):
                    if self.composition.itemData(index)['role']==previous['role']:self.composition.setCurrentIndex(index)
        elif outcome.state=='applied':self.context=None
        self.controls()

    def _apply(self):
        if self.context is not None and self.composition.currentData() is not None:
            self.binding.apply(self.context.token,self.composition.currentData()['role'],self.draft())

    def closeEvent(self,event):
        if self.binding.host.busy:event.ignore()
        else:event.accept()


class CreativeCompanionBinding(QObject):
    def __init__(self,host):
        super().__init__(host.window)
        self.host=host;self.service=WorkspaceCreative(host.controller);self.dialog=None
        self.button=QPushButton('Hook / CTA');self.button.clicked.connect(self.open)
        host.panel.layout().addWidget(self.button)
        self.controls()

    def controls(self):
        state=self.host.controller.status()
        allowed=getattr(self.host.window,'action_allowed',lambda owner:True)(self.host)
        self.button.setEnabled(allowed and not self.host.busy and state.can_checkpoint)
        if self.dialog is not None:self.dialog.controls()

    def open(self):
        if self.host.busy or not self.host.controller.status().can_checkpoint:return
        if self.dialog is None:self.dialog=CreativeDialog(self)
        self.dialog.show();self.dialog.raise_();self.load()

    def _run(self,operation,message):
        host=self.host
        if host.busy or not host._claim():return
        host.busy=True;host.status.setText(message);self.controls();host._refresh()
        def finished(outcome):
            host.busy=False;host._release_if_safe();host.status.setText(outcome.message)
            if self.dialog is not None:self.dialog.refresh(outcome)
            host._refresh();self.controls()
        def failed(_):
            host.busy=False;host._release_if_safe()
            if self.dialog is not None:self.dialog.status.setText('The title action did not finish. Check the editor before retrying.')
            host._refresh();self.controls()
        host.runner.run(lambda job:operation(),on_finished=finished,on_error=failed)

    def load(self):self._run(self.service.context,'Loading the creative title context…')
    def apply(self,token,role,draft):self._run(lambda:self.service.apply(token,role,draft),'Adding the native title…')
