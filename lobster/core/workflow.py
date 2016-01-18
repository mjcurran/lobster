import imp
import logging
import os
import shutil
import sys

from lobster import fs, util
from lobster.cmssw import sandbox
from lobster.core.dataset import ProductionDataset
from lobster.core.task import *

logger = logging.getLogger('lobster.workflow')

class Workflow(object):
    def __init__(self,
            label,
            category=None,
            dataset,
            publish_label=None,
            cores_per_task=1,
            task_runtime=None,
            task_memory=None,
            task_disk=None,
            merge_cleanup=True,
            merge_size=-1,
            sandbox=None,
            sandbox_release=None,
            sandbox_blacklist=None,
            command='cmsRun',
            extra_inputs = None,
            arguments=None,
            unique_arguments=None,
            outputs=None,
            output_format="{base}_{id}.{ext}",
            randomize_seeds=True,
            parent=None,
            local=False,
            cmssw_config=None,
            globaltag=None,
            edm_output=True):
        self.label = label
        self.category = category if category else label
        self.dataset = dataset

        self.publish_label = publish_label if publish_label else label

        self.cores = cores_per_task
        self._runtime = task_runtime
        self.memory = task_memory
        self.disk = task_disk
        self.merge_size = self.__check_merge(merge_size)
        self.merge_cleanup = merge_cleanup

        self.cmd = command
        self.extra_inputs = extra_inputs if extra_inputs else []
        self.args = arguments if arguments else []
        self.unique_args = unique_arguments if unique_arguments else [None]
        self._outputs = outputs
        self.outputformat = output_format

        self.dependents = []
        self.prerequisite = parent

        self.pset = cmssw_config
        self.globaltag = globaltag
        self.local = local or hasattr(dataset, 'files')
        self.edm_output = edm_output

        self.sandbox = sandbox
        if sandbox_release is not None:
            self.sandbox_release = sandbox_release
        else:
            try:
                self.sandbox_release = os.environ['LOCALRT']
            except:
                raise AttributeError("Need to be either in a `cmsenv` or specify a sandbox release!")
        self.sandbox_blacklist = sandbox_blacklist

    @property
    def runtime(self):
        if not self._runtime:
            return None
        return self._runtime + 15 * 60

    def __check_merge(self, size):
        if size <= 0:
            return size

        orig = size
        if isinstance(size, basestring):
            unit = size[-1].lower()
            try:
                size = float(size[:-1])
                if unit == 'k':
                    size *= 1000
                elif unit == 'm':
                    size *= 1e6
                elif unit == 'g':
                    size *= 1e9
                else:
                    size = -1
            except ValueError:
                size = -1
        if size > 0:
            logger.info('merging outputs up to {0} bytes'.format(size))
        else:
            logger.error('merging disabled due to malformed size {0}'.format(orig))
        return size

    def register(self, wflow):
        """Add the workflow `wflow` to the dependents.
        """
        logger.info("marking {0} to be downstream of {1}".format(wflow.label, self.label))
        if len(self._outputs) != 1:
            raise NotImplementedError("dependents for {0} output files not yet supported".format(len(self._outputs)))
        self.dependents.append(wflow.label)

    def copy_inputs(self, basedirs, overwrite=False):
        """Make a copy of extra input files.

        Includes CMSSW parameter set if specified.  Already present files
        will not be overwritten unless specified.
        """
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)

        if self.pset:
            shutil.copy(util.findpath(basedirs, self.pset), os.path.join(self.workdir, os.path.basename(self.pset)))

        if self.extra_inputs is None:
            return []

        def copy_file(fn):
            source = os.path.abspath(util.findpath(basedirs, fn))
            target = os.path.join(self.workdir, os.path.basename(fn))

            if not os.path.exists(target) or overwrite:
                if not os.path.exists(os.path.dirname(target)):
                    os.makedirs(os.path.dirname(target))

                logger.debug("copying '{0}' to '{1}'".format(source, target))
                if os.path.isfile(source):
                    shutil.copy(source, target)
                elif os.path.isdir(source):
                    shutil.copytree(source, target)
                else:
                    raise NotImplementedError

            return target

        files = map(copy_file, self.extra_inputs)
        self.extra_inputs = files

    def determine_outputs(self, basedirs):
        """Determine output files for CMSSW tasks.
        """
        self._outputs = []
        # Save determined outputs to the configuration in the
        # working directory.
        update_config = True

        # To avoid problems loading configs that use the VarParsing module
        sys.argv = [os.path.basename(self.pset)] + self.args
        with open(util.findpath(basedirs, self.pset), 'r') as f:
            source = imp.load_source('cms_config_source', self.pset, f)
            process = source.process
            if hasattr(process, 'GlobalTag') and hasattr(process.GlobalTag.globaltag, 'value'):
                self.global_tag = process.GlobalTag.globaltag.value()
            for label, module in process.outputModules.items():
                self._outputs.append(module.fileName.value())
            if 'TFileService' in process.services:
                self._outputs.append(process.services['TFileService'].fileName.value())
                self.edm_output = False

            logger.info("workflow {0}: adding output file(s) '{1}'".format(self.label, ', '.join(self._outputs)))

    def setup(self, workdir, basedirs):
        self.workdir = os.path.join(workdir, self.label)

        if self.sandbox:
            self.version, self.sandbox = sandbox.recycle(self.sandbox, workdir)
        else:
            self.version, self.sandbox = sandbox.package(
                    self.sandbox_release,
                    workdir,
                    self.sandbox_blacklist)

        self.copy_inputs(basedirs)
        if self.pset and not self._outputs:
            self.determine_outputs(basedirs)

        # Working directory for workflow
        # TODO Should we really check if this already exists?  IMO that
        # constitutes an error, since we really should create the workflow!
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)
        # Create the stageout directory
        if not fs.exists(self.label):
            fs.makedirs(self.label)
        else:
            if len(list(fs.ls(self.label))) > 0:
                msg = 'stageout directory is not empty: {0}'
                raise IOError(msg.format(fs.__getattr__('lfn2pfn')(self.label)))

    def handler(self, id_, files, lumis, taskdir, merge=False):
        if merge:
            return MergeTaskHandler(id_, self.label, files, lumis, list(self.outputs(id_)), taskdir)
        elif isinstance(self.dataset, ProductionDataset):
            return ProductionTaskHandler(id_, self.label, lumis, list(self.outputs(id_)), taskdir)
        else:
            return TaskHandler(id_, self.label, files, lumis, list(self.outputs(id_)), taskdir, local=self.local)

    def outputs(self, id):
        for fn in self._outputs:
            base, ext = os.path.splitext(fn)
            outfn = self.outputformat.format(base=base, ext=ext[1:], id=id)
            yield fn, os.path.join(self.label, outfn)

    def adjust(self, params, taskdir, inputs, outputs, merge, reports=None, unique=None):
        cmd = self.cmd
        args = self.args
        pset = self.pset

        inputs.append((self.sandbox, 'sandbox.tar.bz2', True))
        if merge:
            inputs.append((os.path.join(os.path.dirname(__file__), 'data', 'merge_reports.py'), 'merge_reports.py', True))
            inputs.append((os.path.join(os.path.dirname(__file__), 'data', 'task.py'), 'task.py', True))
            inputs.extend((r, "_".join(os.path.normpath(r).split(os.sep)[-3:]), False) for r in reports)

            if self.edm_output:
                args = ['output=' + self._outputs[0]]
                pset = os.path.join(os.path.dirname(__file__), 'data', 'merge_cfg.py')
            else:
                cmd = 'hadd'
                args = ['-n', '0', '-f', self._outputs[0]]
                pset = None
                params['append inputs to args'] = True

            params['prologue'] = None
            params['epilogue'] = ['python', 'merge_reports.py', 'report.json'] \
                    + ["_".join(os.path.normpath(r).split(os.sep)[-3:]) for r in reports]
        else:
            inputs.extend((i, os.path.basename(i), True) for i in self.extra_inputs)

            if unique:
                args.append(unique)
            if pset:
                pset = os.path.join(self.workdir, pset)
            if self._runtime:
                # cap task runtime at desired runtime + 10 minutes grace
                # period (CMSSW 7.4 and higher only)
                params['task runtime'] = self._runtime + 10 * 60
            params['cores'] = self.cores

        if pset:
            inputs.append((pset, os.path.basename(pset), True))
            outputs.append((os.path.join(taskdir, 'report.xml.gz'), 'report.xml.gz'))

            params['pset'] = os.path.basename(pset)

        params['executable'] = cmd
        params['arguments'] = args
        if isinstance(self.dataset, ProductionDataset):
            params['mask']['events'] = self.dataset.events_per_task
            params['mask']['events per lumi'] = self.dataset.events_per_lumi
            params['randomize seeds'] = self.dataset.randomize_seeds
        else:
            params['mask']['events'] = -1
