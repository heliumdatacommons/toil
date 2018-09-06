from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
import logging
from toil.batchSystems.abstractBatchSystem import (
    AbstractBatchSystem, BatchSystemSupport, BatchSystemLocalSupport)
import chronos
import time
import os
import sys
from threading import Thread
import six
import copy
import httplib
from six.moves.queue import Empty, Queue
from six.moves.urllib.parse import urlparse
logger = logging.getLogger(__name__)

class ChronosBatchSystem(BatchSystemLocalSupport):
    #TODO look at how singleMachine batch system does clean up/shutdown

    @classmethod
    def supportsWorkerCleanup(cls):
        return False

    @classmethod
    def supportsHotDeployment(cls):
        return False

    @classmethod
    def supportsAutoDeployment(cls):
        return False

    def __init__(self, config, maxCores, maxMemory, maxDisk):
        #super(ChronosBatchSystem, self).__init__(config, maxCores, maxMemory, maxDisk)
        super(ChronosBatchSystem, self).__init__(config, maxCores, maxMemory, maxDisk)
        logger.debug("config: {}".format(config))
        c = os.getenv("CHRONOS_URL")
        if not c:
            raise RuntimeError("Chronos batch system requires CHRONOS_URL to be set")
        urlp = urlparse(c)
        if urlp.scheme:
            self.chronos_proto = urlp.scheme
            self.chronos_endpoint = c[len(urlp.scheme) + 3:]
        else:
            self.chronos_proto = "https"
            self.chronos_endpoint = c
        #print("proto: " + str(self.chronos_proto))
        #print("endpoint: " + str(self.chronos_endpoint))
        self.shared_filesystem_password = os.getenv("IRODS_PASSWORD")
        if self.chronos_endpoint is None:
            raise RuntimeError(
                "Chronos batch system requires environment variable "
                "'TOIL_CHRONOS_ENDPOINT' to be defined.")
        if self.chronos_proto is None:
            self.chronos_proto = "http"
        if self.shared_filesystem_password is None:
            raise RuntimeError(
                "Chronos batch system requires a password for shared filesystem")

        def int_from_env_var(varname, defaultval, minval=None, maxval=None):
            val = os.getenv(varname)
            if not val:
                return defaultval
            try:
                val = int(val)
            except ValueError:
                raise ValueError("{} must be a number")
            if minval and val < minval:
                raise RuntimeError("{} must be >= {}".format(varname, minval))
            if maxval and val > maxval:
                raise RuntimeError("{} must be <= {}".format(varname, maxval))
            return val

        self.poll_interval = int_from_env_var("CHRONOS_POLL_INTERVAL", 10)
        self.retry_count = int_from_env_var("CHRONOS_RETRY_COUNT", 30)
        self.retry_interval = int_from_env_var("CHRONOS_RETRY_INTERVAL", 20)

        self.cloud_constraint = os.getenv("TOIL_CLOUD_CONSTRAINT")
        self.host_constraint = os.getenv("TOIL_HOST_CONSTRAINT")
        self.toil_worker_image = os.getenv("TOIL_WORKER_IMAGE", "heliumdatacommons/datacommons-base:latest")
        """
        List of jobs in format:
        { "name": <str>,
           ... other chronos job fields
          "issued_time": <issued time in seconds (Float)>,
          "status": <fresh|success|failed> }
        """
        self.issued_jobs = []

        self.updated_jobs_queue = Queue()
        self.jobStoreID = None
        self.running = True
        self.worker = Thread(target=self.updated_job_worker, args=())
        self.worker.start()

    def updated_job_worker(self):
        # poll chronos api and check for changed job statuses
        client = get_chronos_client(self.chronos_endpoint, self.chronos_proto)
        not_found_counts = {}
        not_found_fail_threshold = 5 # how many times to allow a job to not be found
        while self.running:
            # jobs for the job store of this batch
            #remote_jobs = client.search(name=self.jobStoreID)
            # job summary info, contains status for jobs, which we need
            chronos_domain_name = urlparse(os.environ['CHRONOS_URL']).netloc
            for i in range(self.retry_count):
                try:
                    remote_jobs_summary = client._call("/scheduler/jobs/summary")["jobs"]
                    break
                except (chronos.ChronosAPIError, httplib.ResponseNotReady) as e:
                    os.system('dig ' + chronos_domain_name)
                    print("Caught error in calling Chronos API: {}, trying again [count={}]".format(repr(e), i))
                    if i == self.retry_count - 1:
                        raise e
                    else:
                        time.sleep(self.retry_interval)
            issued = copy.copy(self.issued_jobs)
            logger.info("Checking status of jobs: {}".format(str([j["name"] for j in issued])))
            for cached_job in issued:
                job_name = cached_job["name"]
                remote_job = None
                for j in remote_jobs_summary:
                    if j["name"] == job_name:
                        remote_job = j
                if not remote_job:
                    logger.error("Job '%s' not found in chronos" % job_name)
                    if job_name not in not_found_counts: not_found_counts[job_name] = 0
                    not_found_counts[job_name] += 1
                    if not_found_counts[job_name] > not_found_fail_threshold:
                        raise RuntimeError(
                            "Chronos job not found during {} poll iterations".format(not_found_fail_threshold))
                    else:
                        continue #if not found, could be race condition with chronos REST API job adding
                if remote_job["status"] != cached_job["status"]:
                    cached_job["status"] = remote_job["status"]

                    proc_status = chronos_status_to_proc_status(cached_job["status"])
                    logger.info("Job '{}' updated in Chronos with status '{}'".format(job_name, cached_job["status"]))
                    self.updated_jobs_queue.put(
                        (job_name, proc_status, time.time() - cached_job["issued_time"])
                    )
                    # job is no longer "issued" if it has completed with success or failure
                    logger.debug(str(remote_job))
                    if remote_job["status"] in ["failure", "success"]:
                        self.issued_jobs.remove(cached_job)

            time.sleep(self.poll_interval)

    def setUserScript(self, userScript):
        raise NotImplementedError()


    """
    Currently returning the string name of the chronos job instead of an int id
    """
    def issueBatchJob(self, jobNode):
        logger.info("jobNode: " + str(vars(jobNode)))
        localID = self.handleLocalJob(jobNode)
        if localID:
            return localID
        # store jobStoreID as a way to reference this batch of jobs
        self.jobStoreID = jobNode.jobStoreID.replace("/", "-")
        logger.debug("issuing batch job with unique ID: {}".format(self.jobStoreID))
        logger.debug("jobNode command: {}".format(jobNode.command))
        client = get_chronos_client(self.chronos_endpoint, self.chronos_proto)
        mem = jobNode.memory / 2**20 # B -> MiB
        cpus = jobNode.cores
        disk = jobNode.disk / 2**20 # B -> MiB
        # set default here due to --default options not working when subset of reqs specified in workflow
        if not disk or disk < 20 * 2**10:
            disk = 20 * 2**10 # MiB -> GiB

        logger.info("Requesting resources: mem={}, cpus={}, disk={}".format(mem, cpus, disk))
        # if a job with this name already exists, it will be overwritten in chronos.
        # we don't want this, so increment a unique counter on the end of it.
        simple_jobnode_jobname = jobNode.jobName.split("/")[-1]
        dup_job_name_counter = len(
                [x for x in self.issued_jobs if simple_jobnode_jobname in x["name"]])
        job_name = "{}_{}_{}".format(
                jobNode.jobStoreID, jobNode.jobName.split("/")[-1], dup_job_name_counter)
        job_name = job_name.replace("/", "-")

        # all environment variables in this context that start with IRODS_ will be passed to worker containers
        env_str = ""
        for k,v in six.iteritems(os.environ):
            if k.startswith("IRODS_") or k.startswith("TOIL_") or k == "WORKFLOW_NAME":
                env_str += "-e {}='{}' ".format(k,v)

        job = {
            "name": job_name,
 #           "container": {
 #               "type": "DOCKER",
 #               "image": "heliumdatacommons/datacommons-base",
 #               "network": "BRIDGE",
 #               "forcePullImage": True,
 #               "parameters": [
 #                   { "key": "privileged", "value": True},
 #                   { "key": "rm", "value": True}
 #               ]
 #           },
#            "command": "_toil_worker " + " ".join(jobNode.command.split(" ")[1:]),
#            "environmentVariables": [
#                {"name":str(k), "value":str(v)} for k,v in six.iteritems(os.environ) if k.startswith("IRODS_")
#            ],
            "arguments": [],
            "command": (
                "sudo docker pull {};".format(self.toil_worker_image)
                + "sudo docker run --rm --privileged {} -v /toil-intermediate:/toil-intermediate {} _toil_worker '{}'".format(
                        env_str, # aggregated environment vars
                        self.toil_worker_image,
                        " ".join(jobNode.command.split(" ")[1:])
                    ) # args after original _toil_worker
            ),
            "constraints": [],
            "owner": "",
            "disabled": False,
            "schedule": "R1//P1Y",
            "execute_now": True,
            "shell": True,
            "cpus": cpus,
            "mem": mem,
            "disk": disk
        }

        if self.cloud_constraint:
            logger.info("Setting cloud constraint: " + str(self.cloud_constraint))
            job["constraints"].append(["cloud", "EQUALS", str(self.cloud_constraint)])

        if self.host_constraint:
            logger.info("Setting host constraint: " + str(self.host_constraint))
            job["constraints"].append(["hostname", "EQUALS", str(self.host_constraint)])

        logger.info("Creating job in chronos: \n%s" % job)
        # TODO is this return value relevant?
        chronos_domain_name = urlparse(os.environ['CHRONOS_URL']).netloc
        for i in range(self.retry_count):
            try:
                ret = client.add(job)
                break
            except (chronos.ChronosAPIError, httplib.ResponseNotReady) as e:
                os.system('dig ' + chronos_domain_name)
                print("Caught error in calling Chronos API: {}, trying again [count={}]".format(repr(e), i))
                if i == self.retry_count - 1:
                    raise e
                else:
                    time.sleep(self.retry_interval)

        print(str(ret))
        job["issued_time"] = time.time()
        job["status"] = "fresh" # corresponds to status in chronos for jobs that have not yet run
        self.issued_jobs.append(job)

        return job["name"]


    """
    Kill the tasks for a list of jobs in chronos, and delete the jobs in chronos
    """
    def killBatchJobs(self, jobIDs):
        self.killLocalJobs(jobIDs)
        client = get_chronos_client(self.chronos_endpoint, self.chronos_proto)
        for jobID in jobIDs:
            chronos_domain_name = urlparse(os.environ['CHRONOS_URL']).netloc
            for i in range(self.retry_count):
                try:
                    client.delete_tasks(jobID)
                    client.delete(jobID)
                    logger.info("Removed job '{}' from chronos.".format(jobID))
                    break
                except (chronos.ChronosAPIError, httplib.ResponseNotReady) as e:
                    os.system('dig ' + chronos_domain_name)
                    print("Caught error in calling Chronos API: {}, trying again [count={}]".format(repr(e), i))
                    if i == self.retry_count - 1:
                        raise e
                    else:
                        time.sleep(self.retry_interval)


    """
    Currently returning the string name of the jobs as the ids, not int ids
    Matches ids from issueBatchJob
    """
    def getIssuedBatchJobIDs(self):
        """
        if not self.jobStoreID:
            return []
        client = get_chronos_client(self.chronos_endpoint, self.chronos_proto)
        jobs = client.search(name=self.jobStoreID)
        ids = [j["name"] for j in jobs]
        return ids
        """
        return [j["name"] for j in self.issued_jobs] + list(self.getIssuedLocalJobIDs())

    """
    Returns {<jobname(str)>: <seconds(int)>, ...}
    """
    def getRunningBatchJobIDs(self):
        if not self.jobStoreID:
            return {}
        client = get_chronos_client(self.chronos_endpoint, self.chronos_proto)
        chronos_domain_name = urlparse(os.environ['CHRONOS_URL']).netloc
        for i in range(self.retry_count):
            try:
                jobs = client.search(name=self.jobStoreID)
                jobs_summary = client._call("/scheduler/jobs/summary")["jobs"]
                break
            except (chronos.ChronosAPIError, httplib.ResponseNotReady) as e:
                os.system('dig ' + chronos_domain_name)
                print("Caught error in calling Chronos API: {}, trying again [count={}]".format(repr(e), i))
                if i == self.retry_count - 1:
                    raise e
                else:
                    time.sleep(self.retry_interval)

        running_jobs = {}
        for j in jobs:
            # look for this job in the job summary list (which has the state and status fields)
            for summary in jobs_summary:
                if summary["name"] == j["name"]:
                    # add state field from summary to job obj
                    j["status"] = summary["status"]
                    j["state"] = summary["state"]
                    if "running" in j["state"]:
                        # look up local job obj which contains the issued time and compare to now
                        # (not the actual run time in mesos, just time since it was issued in toil)
                        run_seconds = 0
                        for lj in self.issued_jobs:
                            if lj["name"] == j["name"]:
                                run_seconds = time.time() - lj["issued_time"]
                        running_jobs[j["name"]] = run_seconds
        running_jobs.update(self.getRunningLocalJobIDs())
        return running_jobs

    """
    Returns the most recently updated job. Waits up to maxWait for a job to be marked as updated.
    The worker thread marks a job as updated when its status changes in Chronos.
    """
    def getUpdatedBatchJob(self, maxWait):
        local_tuple = self.getUpdatedLocalJob(0)
        if local_tuple:
            return local_tuple
        while True:
            try:
                job_id, status, wallTime = self.updated_jobs_queue.get(timeout=maxWait)
            except Empty:
                return None

            return job_id, status, wallTime

        return None

    def shutdown(self):
        self.shutdownLocal()
        logger.debug("shutdown called")
        self.running = False
        self.worker.join()

    def setEnv(self, name, value=None):
        raise NotImplementedError()

    @classmethod
    def getRescueBatchJobFrequency(cls):
        raise NotImplementedError()
    @classmethod
    def setOptions(cls, setOption):
        pass

def get_chronos_client(endpoint, proto):
    client = chronos.connect(endpoint, proto=proto)
    return client

def chronos_status_to_proc_status(status):
    if not status or status == "failure":
        return 1
    else:
        return 0
