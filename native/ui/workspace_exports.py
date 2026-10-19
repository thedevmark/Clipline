"""Dual-file export controls on the existing companion action and job owner."""
import threading
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QFileDialog,QHBoxLayout,QLabel,QLineEdit,
    QPlainTextEdit,QProgressBar,QPushButton,QVBoxLayout)


class ExportDialog(QDialog):
    def __init__(self,binding):
        super().__init__(binding.window)
        self.binding=binding
        self.setWindowTitle('Export landscape and portrait')
        self.setMinimumWidth(480);self.resize(620,430)
        body=QVBoxLayout(self)
        intro=QLabel('Save the active edit, then export landscape and portrait with its accepted captions. Both verified MP4 files will be copied to the folder you choose.')
        intro.setWordWrap(True);body.addWidget(intro)
        body.addWidget(QLabel('Output folder'))
        row=QHBoxLayout();self.folder=QLineEdit();self.folder.setAccessibleName('Export output folder')
        row.addWidget(self.folder);self.browse=QPushButton('Choose folder…');row.addWidget(self.browse);body.addLayout(row)
        self.browse.clicked.connect(self._choose)
        self.status=QLabel('Choose an output folder.');self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);body.addWidget(self.status)
        self.progress=QProgressBar();self.progress.setRange(0,0);self.progress.hide();body.addWidget(self.progress)
        self.results=QPlainTextEdit();self.results.setReadOnly(True);self.results.setAccessibleName('Exported files and retained evidence');body.addWidget(self.results)
        buttons=QHBoxLayout();self.start=QPushButton('Save and export both');self.start.clicked.connect(self._start);buttons.addWidget(self.start)
        self.stop=QPushButton('Stop retained export');self.stop.clicked.connect(self._cleanup);buttons.addWidget(self.stop)
        self.cancel=QPushButton('Cancel export');self.cancel.clicked.connect(self._cancel);buttons.addWidget(self.cancel)
        self.done=QPushButton('Done');self.done.clicked.connect(self.accept);buttons.addWidget(self.done);body.addLayout(buttons)
        self.controls()

    def _choose(self):
        path=QFileDialog.getExistingDirectory(self,'Export output folder',self.folder.text())
        if path:self.folder.setText(path)

    def controls(self):
        b=self.binding;service=b.controller._exports
        available=getattr(b.window,'action_allowed',lambda owner:True)(b)
        current=b.controller.status()
        self.start.setEnabled(available and not b.busy and current.can_checkpoint and not service.blocked)
        self.browse.setEnabled(not b.busy);self.folder.setEnabled(not b.busy)
        self.stop.setVisible(service.unsettled);self.stop.setEnabled(available and not b.busy and service.unsettled)
        self.cancel.setVisible(b.busy and b.cancel_label=='Cancel export')
        self.cancel.setEnabled(b.cancel is not None and not b.cancel.is_set())
        self.done.setEnabled(not b.busy)

    def _start(self):
        if not self.folder.text().strip():self.status.setText('Choose an output folder first.');return
        self._run(False)

    def _cleanup(self):self._run(True)

    def _run(self,cleanup):
        b=self.binding
        if b.busy or not b._claim():return
        b.busy=True;b.cancel=threading.Event();b.cancel_label='Cancel export'
        self.progress.show();self.status.setText('Stopping the retained export…' if cleanup else 'Saving the active edit…')
        self.results.clear();b._refresh();self.controls()
        def operation(job):
            if cleanup:return b.controller._exports.cleanup()
            return b.controller._exports.run(self.folder_value,cancel=b.cancel,progress=job.progress.emit)
        self.folder_value=self.folder.text().strip()
        b.runner.run(operation,on_progress=self._progress,on_finished=self._finish,on_error=self._error)

    def _progress(self,message):
        self.status.setText(message);self.binding.status.setText(message)

    def _finish(self,result):
        self.binding.busy=False;self.binding.cancel=None;self.progress.hide()
        self.status.setText(result.message);self.binding.status.setText(result.message)
        rows=[]
        if result.paths:rows.extend(['Saved files:',*result.paths])
        if result.originals:rows.extend(['','Verified originals:',*result.originals])
        if result.evidence:rows.extend(['','Export evidence:',result.evidence])
        self.results.setPlainText('\n'.join(rows));self.binding._release_if_safe();self.binding._refresh();self.controls()

    def _error(self,message):
        self.binding.busy=False;self.binding.cancel=None;self.progress.hide()
        self.status.setText(message);self.binding.status.setText(message)
        self.binding._release_if_safe();self.binding._refresh();self.controls()

    def _cancel(self):
        if self.binding.cancel is not None:
            self.binding.cancel.set();self.status.setText('Cancelling export. Existing verified files will be kept.');self.controls()

    def reject(self):
        if not self.binding.busy:super().reject()

    def closeEvent(self,event):
        if self.binding.busy:event.ignore()
        else:super().closeEvent(event)
