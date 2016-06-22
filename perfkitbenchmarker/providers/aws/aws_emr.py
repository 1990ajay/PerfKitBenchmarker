# Copyright 2016 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module containing class for AWS's spark service.

Spark clusters can be created and deleted.
"""

import json
import logging

from perfkitbenchmarker import flags
from perfkitbenchmarker import providers
from perfkitbenchmarker import spark_service
from perfkitbenchmarker import vm_util
import time
import util


FLAGS = flags.FLAGS

DEFAULT_MACHINE_TYPE = 'm1.large'
RELEASE_LABEL = 'emr-4.5.0'
READY_CHECK_SLEEP = 30
READY_CHECK_TRIES = 60
READY_STATE = 'WAITING'

JOB_WAIT_SLEEP = 30

DELETED_STATES = ['TERMINATING', 'TERMINATED_WITH_ERRORS', 'TERMINATED']


class AwsEMR(spark_service.BaseSparkService):
  """Object representing a AWS EMR cluster.

  Attributes:
    cluster_id: Cluster identifier, set in superclass.
    num_workers: Number of works, set in superclass.
    machine_type: Machine type to use.
    project: Enclosing project for the cluster.
    cmd_prefix: emr prefix, including region
    region: region in which cluster is located.
  """

  CLOUD = providers.AWS
  SERVICE_NAME = 'emr'

  def __init__(self, spark_service_spec):
    super(AwsEMR, self).__init__(spark_service_spec)
    # TODO(hildrum) use availability zone when appropriate
    if self.machine_type is None:
      self.machine_type = DEFAULT_MACHINE_TYPE
    self.cmd_prefix = util.AWS_PREFIX + ['emr']
    self.bucket_prefix = util.AWS_PREFIX + ['s3']
    if FLAGS.zones:
      region = util.GetRegionFromZone(FLAGS.zones[0])
      self.cmd_prefix += ['--region', region]
      self.bucket_prefix += ['--region', region]
    self.bucket_to_delete = None

  def _CreateLogBucket(self):
    bucket_name = 's3://pkb-{0}-emr'.format(FLAGS.run_uri)
    cmd = self.bucket_prefix + ['mb', bucket_name]
    _, _, rc = vm_util.IssueCommand(cmd)
    if rc != 0:
      raise Exception('Error creating logs bucket')
    self.bucket_to_delete = bucket_name
    return bucket_name

  def _Create(self):
    """Creates the cluster."""
    name = 'pkb_' + FLAGS.run_uri
    logs_bucket = FLAGS.aws_emr_loguri or self._CreateLogBucket()
    # we need to store the cluster id.
    cmd = self.cmd_prefix + ['create-cluster', '--name', name,
                             '--release-label', RELEASE_LABEL,
                             '--use-default-roles',
                             '--instance-count', str(self.num_workers),
                             '--instance-type', self.machine_type,
                             '--application', 'Name=SPARK',
                             '--log-uri', logs_bucket]
    stdout, _, _ = vm_util.IssueCommand(cmd)
    result = json.loads(stdout)
    self.cluster_id = result['ClusterId']
    logging.info('Cluster created with id %s', self.cluster_id)

  def _Delete(self):
    """Deletes the cluster."""
    cmd = self.cmd_prefix + ['terminate-clusters', '--cluster-ids',
                             self.cluster_id]
    vm_util.IssueCommand(cmd)
    if self.bucket_to_delete:
      bucket_del_cmd = self.bucket_prefix + ['rb', '--force',
                                             self.bucket_to_delete]
      vm_util.IssueCommand(bucket_del_cmd)

  def _Exists(self):
    """Check to see whether the cluster exists."""
    cmd = self.cmd_prefix + ['describe-cluster',
                             '--cluster-id', self.cluster_id]
    stdout, _, rc = vm_util.IssueCommand(cmd)
    if rc != 0:
      return False
    result = json.loads(stdout)
    if result['Cluster']['Status']['State'] in DELETED_STATES:
      return False
    else:
      return True

  def _IsReady(self):
    """Check to see if the cluster is ready."""
    cmd = self.cmd_prefix + ['describe-cluster', '--cluster-id',
                             self.cluster_id]
    stdout, _, rc = vm_util.IssueCommand(cmd)
    result = json.loads(stdout)
    if result['Cluster']['Status']['State'] == READY_STATE:
      return True
    else:
      return False

  def _GetLogBase(self):
    """Gets the base uri for the logs."""
    cmd = self.cmd_prefix + ['describe-cluster', '--cluster-id',
                             self.cluster_id]
    stdout, _, _ = vm_util.IssueCommand(cmd)
    result = json.loads(stdout)
    if 'LogUri' in result['Cluster']:
      self.logging_enabled = True
      log_uri = result['Cluster']['LogUri']
      if log_uri.startswith('s3n'):
        log_uri = 's3' + log_uri[3:]
      return log_uri
    else:
      return None


  @vm_util.Retry()
  def _CheckForFile(self, filename):
    """Wait for file to appear on s3."""
    cmd = self.bucket_prefix + ['ls', filename]
    _, _, rc = vm_util.IssueCommand(cmd)
    return rc == 0

  def _IsStepDone(self, step_id):
    """Determine whether the step is done.

    Args:
      step_id: The step id to query.
    Returns:
      A dictionary describing the step if the step the step is complete,
          None otherwise.
    """

    cmd = self.cmd_prefix + ['describe-step', '--cluster-id',
                             self.cluster_id, '--step-id', step_id]
    stdout, _, _ = vm_util.IssueCommand(cmd)
    result = json.loads(stdout)
    state = result['Step']['Status']['State']
    if state == "COMPLETED" or state == "FAILED":
      return result
    else:
      return None

  def SubmitJob(self, jarfile, classname, job_poll_interval=JOB_WAIT_SLEEP,
                job_arguments=None, job_stdout_file=None):
    """Submit the job.

    Submit the job and wait for it to complete.  If job_stdout_file is not
    None, also way for the job's stdout to appear and put that in
    job_stdout_file.

    Args:
      jarfile: Jar file containing the class to submit.
      classname: Name of the class.
      job_poll_interval: Submit job will poll until the job is done; this is
          the time between checks.
      job_arguments: Arguments to pass to the job.
      job_stdout_file: Name of a file in which to put the job's standard out.
          If there is data here already, it will be overwritten.

    """

    @vm_util.Retry(poll_interval=job_poll_interval, fuzz=0)
    def WaitForFile(filename):
      if not self._CheckForFile(filename):
        raise Exception('File not found yet')

    @vm_util.Retry(poll_interval=job_poll_interval, fuzz=0)
    def WaitForStep(step_id):
      result = self._IsStepDone(step_id)
      if result is None:
        raise Exception('Step {0} not complete.'.format(step_id))
      return result

    arg_list = ['--class', classname, jarfile]
    if job_arguments:
      arg_list += job_arguments
    arg_string = '[' + ','.join(arg_list) + ']'
    step_list = ['Type=Spark', 'Args=' + arg_string]
    step_string = ','.join(step_list)
    cmd = self.cmd_prefix + ['add-steps', '--cluster-id',
                             self.cluster_id, '--steps', step_string]
    stdout, _, _ = vm_util.IssueCommand(cmd)
    result = json.loads(stdout)
    step_id = result['StepIds'][0]
    metrics = {}

    result = WaitForStep(step_id)
    pending_time = result['Step']['Status']['Timeline']['CreationDateTime']
    start_time = result['Step']['Status']['Timeline']['StartDateTime']
    end_time = result['Step']['Status']['Timeline']['EndDateTime']
    metrics[spark_service.WAITING] = start_time - pending_time
    metrics[spark_service.RUNTIME] = end_time - start_time
    step_state = result['Step']['Status']['State']
    metrics[spark_service.SUCCESS] = step_state == "COMPLETED"

    # Now we need to take the standard out and put it in the designated path,
    # if appropriate.
    if job_stdout_file:
      log_base = self._GetLogBase()
      if log_base is None:
        logging.warn('SubmitJob requested output, but EMR cluster was not '
                     'created with logging')
        return metrics

      # log_base ends in a slash.
      s3_stdout = '{0}{1}/steps/{2}/stdout.gz'.format(log_base,
                                                      self.cluster_id,
                                                      step_id)
      WaitForFile(s3_stdout)
      dest_file = '{0}.gz'.format(job_stdout_file)
      cp_cmd = ['aws', 's3', 'cp', s3_stdout, dest_file]
      _, _, rc = vm_util.IssueCommand(cp_cmd)
      if rc == 0:
        uncompress_cmd = ['gunzip', '-f', dest_file]
        vm_util.IssueCommand(uncompress_cmd)
    return metrics

  def SetClusterProperty(self):
    pass
