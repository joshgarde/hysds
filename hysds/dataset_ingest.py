from __future__ import division
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import absolute_import


from builtins import open
from builtins import str
from future import standard_library

standard_library.install_aliases()
import os
import sys
import re
import traceback
import json
import requests
import shutil
import types
import socket
import backoff
import math
from subprocess import check_output, check_call
from fabric.api import env, get, run, put
from fabric.contrib.files import exists
from pprint import pprint, pformat
from urllib.parse import urlparse
from lxml.etree import parse
from io import StringIO
from glob import glob
from datetime import datetime
from filechunkio import FileChunkIO
from tempfile import mkdtemp

import hysds
import osaka
from hysds.utils import get_disk_usage, makedirs, get_job_status, dataset_exists, get_func, parse_iso8601, \
    log_prov_es, find_dataset_json
from hysds.log_utils import (
    logger,
    log_publish_prov_es,
    backoff_max_value,
    backoff_max_tries,
    log_custom_event,
)
from hysds.recognize import Recognizer
from hysds.orchestrator import do_submit_job
from hysds.celery import app


FILE_RE = re.compile(r"file://(.*?)(/.*)$")
SCRIPT_RE = re.compile(r"script:(.*)$")
BROWSE_RE = re.compile(r"^(.+)\.browse\.png$")


def verify_dataset(dataset):
    """Verify dataset JSON fields."""

    if "version" not in dataset:
        raise RuntimeError("Failed to find required field: version")
    for field in ("label", "location", "starttime", "endtime", "creation_timestamp"):
        if field not in dataset:
            logger.info("Optional field not found: %s" % field)


@backoff.on_exception(
    backoff.expo,
    (RuntimeError, requests.RequestException),
    max_tries=backoff_max_tries,
    max_value=backoff_max_value,
)
def index_dataset(grq_update_url, update_json):
    """Index dataset into GRQ ES."""

    r = requests.post(
        grq_update_url, verify=False, data={"dataset_info": json.dumps(update_json)}
    )
    if not 200 <= r.status_code < 300:
        raise RuntimeError(r.text)
    return r.json()


def queue_dataset(dataset, update_json, queue_name):
    """Add dataset type and URL to queue."""

    payload = {"job_type": "dataset:%s" % dataset, "payload": update_json}
    do_submit_job(payload, queue_name)


def get_remote_dav(url):
    """Get remote dir/file."""

    lpath = "./%s" % os.path.basename(url)
    if not url.endswith("/"):
        url += "/"
    parsed_url = urlparse(url)
    rpath = parsed_url.path
    r = requests.request("PROPFIND", url, verify=False)
    if r.status_code not in (200, 207):  # handle multistatus (207) as well
        logger.info("Got status code %d trying to read %s" % (r.status_code, url))
        logger.info("Content:\n%s" % r.text)
        r.raise_for_status()
    tree = parse(StringIO(r.content))
    makedirs(lpath)
    for elem in tree.findall("{DAV:}response"):
        collection = elem.find(
            "{DAV:}propstat/{DAV:}prop/{DAV:}resourcetype/{DAV:}collection"
        )
        if collection is not None:
            continue
        href = elem.find("{DAV:}href").text
        rel_path = os.path.relpath(href, rpath)
        file_url = os.path.join(url, rel_path)
        local_path = os.path.join(lpath, rel_path)
        local_dir = os.path.dirname(local_path)
        makedirs(local_dir)
        resp = requests.request("GET", file_url, verify=False, stream=True)
        if resp.status_code != 200:
            logger.info(
                "Got status code %d trying to read %s" % (resp.status_code, file_url)
            )
            logger.info("Content:\n%s" % resp.text)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)
                    f.flush()
    return os.path.abspath(lpath)


def get_remote(host, rpath):
    """Get remote dir/file."""

    env.host_string = host
    env.abort_on_prompts = True
    r = get(rpath, ".")
    return os.path.abspath("./%s" % os.path.basename(rpath))


def move_remote_path(host, src, dest):
    """Move remote directory safely."""

    env.host_string = host
    env.abort_on_prompts = True
    dest_dir = os.path.dirname(dest)
    if not exists(dest_dir):
        run("mkdir -p %s" % dest_dir)
    ret = run("mv -f %s %s" % (src, dest))
    return ret


def restage(host, src, dest, signal_file):
    """Restage dataset and create signal file."""

    env.host_string = host
    env.abort_on_prompts = True
    dest_dir = os.path.dirname(dest)
    if not exists(dest_dir):
        run("mkdir -p %s" % dest_dir)
    run("mv -f %s %s" % (src, dest))
    ret = run("touch %s" % signal_file)
    return ret


class NoClobberPublishContextException(Exception):
    pass


def publish_dataset(prod_dir, dataset_file, job, ctx):
    """Publish a dataset. Track metrics."""

    # get job info
    job_dir = job["job_info"]["job_dir"]
    time_start_iso = job["job_info"]["time_start"]
    context_file = job["job_info"]["context_file"]
    datasets_cfg_file = job["job_info"]["datasets_cfg_file"]

    # time start
    time_start = parse_iso8601(time_start_iso)

    # check for PROV-ES JSON from PGE; if exists, append related PROV-ES info;
    # also overwrite merged PROV-ES JSON file
    prod_id = os.path.basename(prod_dir)
    prov_es_file = os.path.join(prod_dir, "%s.prov_es.json" % prod_id)
    prov_es_info = {}
    if os.path.exists(prov_es_file):
        with open(prov_es_file) as f:
            try:
                prov_es_info = json.load(f)
            except Exception as e:
                tb = traceback.format_exc()
                raise RuntimeError(
                    "Failed to log PROV-ES from {}: {}\n{}".format(
                        prov_es_file, str(e), tb
                    )
                )
        log_prov_es(job, prov_es_info, prov_es_file)

    # copy _context.json
    prod_context_file = os.path.join(prod_dir, "%s.context.json" % prod_id)
    shutil.copy(context_file, prod_context_file)

    # force ingest? (i.e. disable no-clobber)
    ingest_kwargs = {"force": False}
    if ctx.get("_force_ingest", False):
        logger.info("Flag _force_ingest set to True.")
        ingest_kwargs["force"] = True

    # upload
    tx_t1 = datetime.utcnow()

    metrics, prod_json = ingest(
        *(
            prod_id,
            datasets_cfg_file,
            app.conf.GRQ_UPDATE_URL,
            app.conf.DATASET_PROCESSED_QUEUE,
            prod_dir,
            job_dir,
        ),
        **ingest_kwargs
    )
    tx_t2 = datetime.utcnow()
    tx_dur = (tx_t2 - tx_t1).total_seconds()
    prod_dir_usage = get_disk_usage(prod_dir)

    # set product provenance
    prod_prov = {
        "product_type": metrics["ipath"],
        "processing_start_time": time_start.isoformat() + "Z",
        "availability_time": tx_t2.isoformat() + "Z",
        "processing_latency": (tx_t2 - time_start).total_seconds() / 60.0,
        "total_latency": (tx_t2 - time_start).total_seconds() / 60.0,
    }
    prod_prov_file = os.path.join(prod_dir, "%s.prod_prov.json" % prod_id)
    if os.path.exists(prod_prov_file):
        with open(prod_prov_file) as f:
            prod_prov.update(json.load(f))
    if "acquisition_start_time" in prod_prov:
        if "source_production_time" in prod_prov:
            prod_prov["ground_system_latency"] = (
                                                         parse_iso8601(prod_prov["source_production_time"])
                                                         - parse_iso8601(prod_prov["acquisition_start_time"])
                                                 ).total_seconds() / 60.0
            prod_prov["total_latency"] += prod_prov["ground_system_latency"]
            prod_prov["access_latency"] = (
                                                  tx_t2 - parse_iso8601(prod_prov["source_production_time"])
                                          ).total_seconds() / 60.0
            prod_prov["total_latency"] += prod_prov["access_latency"]
    # write product provenance of the last product; not writing to an array under the
    # product because kibana table panel won't show them correctly:
    # https://github.com/elasticsearch/kibana/issues/998
    job["job_info"]["metrics"]["product_provenance"] = prod_prov

    job["job_info"]["metrics"]["products_staged"].append(
        {
            "path": prod_dir,
            "disk_usage": prod_dir_usage,
            "time_start": tx_t1.isoformat() + "Z",
            "time_end": tx_t2.isoformat() + "Z",
            "duration": tx_dur,
            "transfer_rate": prod_dir_usage / tx_dur,
            "id": prod_json["id"],
            "urls": prod_json["urls"],
            "browse_urls": prod_json["browse_urls"],
            "dataset": prod_json["dataset"],
            "ipath": prod_json["ipath"],
            "system_version": prod_json["system_version"],
            "dataset_level": prod_json["dataset_level"],
            "dataset_type": prod_json["dataset_type"],
            "index": prod_json["grq_index_result"]["index"],
        }
    )

    return prod_json


def publish_datasets(job, ctx):
    """Perform dataset publishing if job exited with zero status code."""

    # if exit code of job command is non-zero, don't publish anything
    exit_code = job["job_info"]["status"]
    if exit_code != 0:
        logger.info(
            "Job exited with exit code %s. Bypassing dataset publishing." % exit_code
        )
        return True

    # if job command never ran, don't publish anything
    pid = job["job_info"]["pid"]
    if pid == 0:
        logger.info("Job command never ran. Bypassing dataset publishing.")
        return True

    # get job info
    job_dir = job["job_info"]["job_dir"]

    # find and publish
    published_prods = []

    for dataset_file, prod_dir in find_dataset_json(job_dir):

        # skip if marked as localized input
        signal_file = os.path.join(prod_dir, ".localized")
        if os.path.exists(signal_file):
            logger.info("Skipping publish of %s. Marked as localized input." % prod_dir)
            continue

        # publish
        prod_json = publish_dataset(prod_dir, dataset_file, job, ctx)

        # save json for published product
        published_prods.append(prod_json)

    # write published products to file
    pub_prods_file = os.path.join(job_dir, "_datasets.json")
    with open(pub_prods_file, "w") as f:
        json.dump(published_prods, f, indent=2, sort_keys=True)

    # signal run_job() to continue
    return True


# TODO: this used to be called publish_dataset()
def write_to_object_store(
    path, url, params=None, force=False, publ_ctx_file=None, publ_ctx_url=None
):
    """
    Publish a dataset to the given url
    @param path - path of dataset to publish
    @param url - url to publish to
    @param force - unpublish dataset first if exists
    @param publ_ctx_file - publish context file
    @param publ_ctx_url - url to publish context file to
    """

    # set osaka params
    if params is None:
        params = {}

    # force remove previous dataset if it exists?
    if force:
        try:
            delete_from_object_store(url, params=params)
        except:
            pass

    # write publish context file
    if publ_ctx_file is not None and publ_ctx_url is not None:
        try:
            osaka.main.put(publ_ctx_file, publ_ctx_url, params=params, noclobber=True)
        except osaka.utils.NoClobberException as e:
            raise NoClobberPublishContextException(
                "Failed to clobber {} when noclobber is True.".format(publ_ctx_url)
            )

    # upload datasets
    for root, dirs, files in os.walk(path):
        for file in files:
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, path)
            dest_url = os.path.join(url, rel_path)
            logger.info("Uploading %s to %s." % (abs_path, dest_url))
            osaka.main.put(abs_path, dest_url, params=params, noclobber=True)


# TODO: this used to be called unpublish_dataset()
def delete_from_object_store(url, params=None):
    """
    Remove a dataset at (and below) the given url
    @param url - url to remove files (at and below)
    """

    # set osaka params
    if params is None:
        params = {}

    osaka.main.rmall(url, params=params)


def ingest(
    objectid,
    dsets_file,
    grq_update_url,
    dataset_processed_queue,
    prod_path,
    job_path,
    dry_run=False,
    force=False,
):
    """Run dataset ingest."""
    logger.info("#" * 80)
    logger.info("datasets: %s" % dsets_file)
    logger.info("grq_update_url: %s" % grq_update_url)
    logger.info("dataset_processed_queue: %s" % dataset_processed_queue)
    logger.info("prod_path: %s" % prod_path)
    logger.info("job_path: %s" % job_path)
    logger.info("dry_run: %s" % dry_run)
    logger.info("force: %s" % force)

    # get default job path
    if job_path is None:
        job_path = os.getcwd()

    # detect job info
    job = {}
    job_json = os.path.join(job_path, "_job.json")
    if os.path.exists(job_json):
        with open(job_json) as f:
            try:
                job = json.load(f)
            except Exception as e:
                logger.warn("Failed to read job json:\n{}".format(str(e)))
    task_id = job.get("task_id", None)
    payload_id = (
        job.get("job_info", {}).get("job_payload", {}).get("payload_task_id", None)
    )
    payload_hash = job.get("job_info", {}).get("payload_hash", None)
    logger.info("task_id: %s" % task_id)
    logger.info("payload_id: %s" % payload_id)
    logger.info("payload_hash: %s" % payload_hash)

    # get dataset
    if os.path.isdir(prod_path):
        local_prod_path = prod_path
    else:
        local_prod_path = get_remote_dav(prod_path)
    if not os.path.isdir(local_prod_path):
        raise RuntimeError(
            "Failed to find local dataset directory: %s" % local_prod_path
        )

    # write publish context
    publ_ctx_name = "_publish.context.json"
    publ_ctx_dir = mkdtemp(prefix=".pub_context", dir=job_path)
    publ_ctx_file = os.path.join(publ_ctx_dir, publ_ctx_name)
    with open(publ_ctx_file, "w") as f:
        json.dump(
            {
                "payload_id": payload_id,
                "payload_hash": payload_hash,
                "task_id": task_id,
            },
            f,
            indent=2,
            sort_keys=True,
        )
    publ_ctx_url = None

    # dataset name
    pname = os.path.basename(local_prod_path)

    # dataset file
    dataset_file = os.path.join(local_prod_path, "%s.dataset.json" % pname)

    # get dataset json
    with open(dataset_file) as f:
        dataset = json.load(f)
    logger.info("Loaded dataset JSON from file: %s" % dataset_file)

    # check minimum requirements for dataset JSON
    logger.info("Verifying dataset JSON...")
    verify_dataset(dataset)
    logger.info("Dataset JSON verfication succeeded.")

    # get version
    version = dataset["version"]

    # recognize
    r = Recognizer(dsets_file, local_prod_path, objectid, version)

    # get extractor
    extractor = r.getMetadataExtractor()
    if extractor is not None:
        match = SCRIPT_RE.search(extractor)
        if match:
            extractor = match.group(1)
    logger.info("Configured metadata extractor: %s" % extractor)

    # metadata file
    metadata_file = os.path.join(local_prod_path, "%s.met.json" % pname)

    # metadata seed file
    seed_file = os.path.join(local_prod_path, "met.json")

    # metadata file already here
    if os.path.exists(metadata_file):
        with open(metadata_file) as f:
            metadata = json.load(f)
        logger.info("Loaded metadata from existing file: %s" % metadata_file)
    else:
        if extractor is None:
            logger.info("No metadata extraction configured. Setting empty metadata.")
            metadata = {}
        else:
            logger.info(
                "Running metadata extractor %s on %s" % (extractor, local_prod_path)
            )
            m = check_output([extractor, local_prod_path])
            logger.info("Output: %s" % m.decode())

            # generate json to update metadata and urls
            metadata = json.loads(m)

            # set data_product_name
            metadata["data_product_name"] = objectid

            # merge with seed metadata
            if os.path.exists(seed_file):
                with open(seed_file) as f:
                    seed = json.load(f)
                metadata.update(seed)
                logger.info("Loaded seed metadata from file: %s" % seed_file)

            # write it out to file
            with open(metadata_file, "w") as f:
                json.dump(metadata, f, indent=2)
            logger.info("Wrote metadata to %s" % metadata_file)

            # delete seed file
            if os.path.exists(seed_file):
                os.unlink(seed_file)
                logger.info("Deleted seed file %s." % seed_file)

    # read context
    context_file = os.path.join(local_prod_path, "%s.context.json" % pname)
    if os.path.exists(context_file):
        with open(context_file) as f:
            context = json.load(f)
        logger.info("Loaded context from existing file: %s" % context_file)
    else:
        context = {}

    # set metadata and dataset groups in recognizer
    r.setDataset(dataset)
    r.setMetadata(metadata)

    # get ipath
    ipath = r.getIpath()

    # get level
    level = r.getLevel()

    # get type
    dtype = r.getType()

    # set product metrics
    prod_metrics = {"ipath": ipath, "path": local_prod_path}

    # publish dataset
    if r.publishConfigured():
        logger.info("Dataset publish is configured.")

        # get publish path
        pub_path_url = r.getPublishPath()

        # get publish urls
        pub_urls = [i for i in r.getPublishUrls()]

        # get S3 profile name and api keys for dataset publishing
        s3_secret_key, s3_access_key = r.getS3Keys()
        s3_profile = r.getS3Profile()

        # set osaka params
        osaka_params = {}

        # S3 profile takes precedence over explicit api keys
        if s3_profile is not None:
            osaka_params["profile_name"] = s3_profile
        else:
            if s3_secret_key is not None and s3_access_key is not None:
                osaka_params["aws_access_key_id"] = s3_access_key
                osaka_params["aws_secret_access_key"] = s3_secret_key

        # get pub host and path
        logger.info("Configured pub host & path: %s" % (pub_path_url))

        # check scheme
        if not osaka.main.supported(pub_path_url):
            raise RuntimeError(
                "Scheme %s is currently not supported." % urlparse(pub_path_url).scheme
            )

        # upload dataset to repo; track disk usage and start/end times of transfer
        prod_dir_usage = get_disk_usage(local_prod_path)
        tx_t1 = datetime.utcnow()
        if dry_run:
            logger.info("Would've published %s to %s" % (local_prod_path, pub_path_url))
        else:
            publ_ctx_url = os.path.join(pub_path_url, publ_ctx_name)
            orig_publ_ctx_file = publ_ctx_file + ".orig"
            try:
                write_to_object_store(
                    local_prod_path,
                    pub_path_url,
                    params=osaka_params,
                    force=force,
                    publ_ctx_file=publ_ctx_file,
                    publ_ctx_url=publ_ctx_url,
                )
            except NoClobberPublishContextException as e:
                logger.warn(
                    "A publish context file was found at {}. Retrieving.".format(
                        publ_ctx_url
                    )
                )
                osaka.main.get(publ_ctx_url, orig_publ_ctx_file, params=osaka_params)
                with open(orig_publ_ctx_file) as f:
                    orig_publ_ctx = json.load(f)
                logger.warn(
                    "original publish context: {}".format(
                        json.dumps(orig_publ_ctx, indent=2, sort_keys=True)
                    )
                )
                orig_payload_id = orig_publ_ctx.get("payload_id", None)
                orig_payload_hash = orig_publ_ctx.get("payload_hash", None)
                orig_task_id = orig_publ_ctx.get("task_id", None)
                logger.warn("orig payload_id: {}".format(orig_payload_id))
                logger.warn("orig payload_hash: {}".format(orig_payload_hash))
                logger.warn("orig task_id: {}".format(orig_payload_id))

                if orig_payload_id is None:
                    raise

                # overwrite if this job is a retry of the previous job
                if payload_id is not None and payload_id == orig_payload_id:
                    msg = (
                        "This job is a retry of a previous job that resulted "
                        + "in an orphaned dataset. Forcing publish."
                    )
                    logger.warn(msg)
                    log_custom_event(
                        "orphaned_dataset-retry_previous_failed",
                        "clobber",
                        {
                            "orphan_info": {
                                "payload_id": payload_id,
                                "payload_hash": payload_hash,
                                "task_id": task_id,
                                "orig_payload_id": orig_payload_id,
                                "orig_payload_hash": orig_payload_hash,
                                "orig_task_id": orig_task_id,
                                "dataset_id": objectid,
                                "dataset_url": pub_path_url,
                                "msg": msg,
                            }
                        },
                    )
                else:
                    job_status = get_job_status(orig_payload_id)
                    logger.warn("orig job status: {}".format(job_status))

                    # overwrite if previous job failed
                    if job_status == "job-failed":
                        msg = (
                            "Detected previous job failure that resulted in an "
                            + "orphaned dataset. Forcing publish."
                        )
                        logger.warn(msg)
                        log_custom_event(
                            "orphaned_dataset-job_failed",
                            "clobber",
                            {
                                "orphan_info": {
                                    "payload_id": payload_id,
                                    "payload_hash": payload_hash,
                                    "task_id": task_id,
                                    "orig_payload_id": orig_payload_id,
                                    "orig_payload_hash": orig_payload_hash,
                                    "orig_task_id": orig_task_id,
                                    "orig_status": job_status,
                                    "dataset_id": objectid,
                                    "dataset_url": pub_path_url,
                                    "msg": msg,
                                }
                            },
                        )
                    else:
                        # overwrite if dataset doesn't exist in grq
                        if not dataset_exists(objectid):
                            msg = "Detected orphaned dataset without ES doc. Forcing publish."
                            logger.warn(msg)
                            log_custom_event(
                                "orphaned_dataset-no_es_doc",
                                "clobber",
                                {
                                    "orphan_info": {
                                        "payload_id": payload_id,
                                        "payload_hash": payload_hash,
                                        "task_id": task_id,
                                        "dataset_id": objectid,
                                        "dataset_url": pub_path_url,
                                        "msg": msg,
                                    }
                                },
                            )
                        else:
                            raise
                write_to_object_store(
                    local_prod_path,
                    pub_path_url,
                    params=osaka_params,
                    force=True,
                    publ_ctx_file=publ_ctx_file,
                    publ_ctx_url=publ_ctx_url,
                )
            except osaka.utils.NoClobberException as e:
                if dataset_exists(objectid):
                    try:
                        osaka.main.rmall(publ_ctx_url, params=osaka_params)
                    except:
                        logger.warn(
                            "Failed to clean up publish context {} after attempting to clobber valid dataset.".format(
                                publ_ctx_url
                            )
                        )
                    raise
                else:
                    msg = "Detected orphaned dataset without ES doc. Forcing publish."
                    logger.warn(msg)
                    log_custom_event(
                        "orphaned_dataset-no_es_doc",
                        "clobber",
                        {
                            "orphan_info": {
                                "payload_id": payload_id,
                                "payload_hash": payload_hash,
                                "task_id": task_id,
                                "dataset_id": objectid,
                                "dataset_url": pub_path_url,
                                "msg": msg,
                            }
                        },
                    )
                    write_to_object_store(
                        local_prod_path,
                        pub_path_url,
                        params=osaka_params,
                        force=True,
                        publ_ctx_file=publ_ctx_file,
                        publ_ctx_url=publ_ctx_url,
                    )
        tx_t2 = datetime.utcnow()
        tx_dur = (tx_t2 - tx_t1).total_seconds()

        # save dataset metrics on size and transfer
        prod_metrics.update(
            {
                "url": urlparse(pub_path_url).path,
                "disk_usage": prod_dir_usage,
                "time_start": tx_t1.isoformat() + "Z",
                "time_end": tx_t2.isoformat() + "Z",
                "duration": tx_dur,
                "transfer_rate": prod_dir_usage / tx_dur,
            }
        )
    else:
        logger.info("Dataset publish is not configured.")
        pub_urls = []

    # publish browse
    if r.browseConfigured():
        logger.info("Browse publish is configured.")

        # get browse path and urls
        browse_path = r.getBrowsePath()
        browse_urls = r.getBrowseUrls()

        # get S3 profile name and api keys for browse image publishing
        s3_secret_key_browse, s3_access_key_browse = r.getS3Keys("browse")
        s3_profile_browse = r.getS3Profile("browse")

        # set osaka params for browse
        osaka_params_browse = {}

        # S3 profile takes precedence over explicit api keys
        if s3_profile_browse is not None:
            osaka_params_browse["profile_name"] = s3_profile_browse
        else:
            if s3_secret_key_browse is not None and s3_access_key_browse is not None:
                osaka_params_browse["aws_access_key_id"] = s3_access_key_browse
                osaka_params_browse["aws_secret_access_key"] = s3_secret_key_browse

        # add metadata for all browse images and upload to browse location
        imgs_metadata = []
        imgs = glob("%s/*browse.png" % local_prod_path)
        for img in imgs:
            img_metadata = {"img": os.path.basename(img)}
            small_img = img.replace("browse.png", "browse_small.png")
            if os.path.exists(small_img):
                small_img_basename = os.path.basename(small_img)
                if browse_path is not None:
                    this_browse_path = os.path.join(browse_path, small_img_basename)
                    if dry_run:
                        logger.info(
                            "Would've uploaded %s to %s" % (small_img, browse_path)
                        )
                    else:
                        logger.info("Uploading %s to %s" % (small_img, browse_path))
                        osaka.main.put(
                            small_img,
                            this_browse_path,
                            params=osaka_params_browse,
                            noclobber=False,
                        )
            else:
                small_img_basename = None
            img_metadata["small_img"] = small_img_basename
            tooltip_match = BROWSE_RE.search(img_metadata["img"])
            if tooltip_match:
                img_metadata["tooltip"] = tooltip_match.group(1)
            else:
                img_metadata["tooltip"] = ""
            imgs_metadata.append(img_metadata)

        # sort browse images
        browse_sort_order = r.getBrowseSortOrder()
        if isinstance(browse_sort_order, list) and len(browse_sort_order) > 0:
            bso_regexes = [re.compile(i) for i in browse_sort_order]
            sorter = {}
            unrecognized = []
            for img in imgs_metadata:
                matched = None
                for i, bso_re in enumerate(bso_regexes):
                    if bso_re.search(img["img"]):
                        matched = img
                        sorter[i] = matched
                        break
                if matched is None:
                    unrecognized.append(img)
            imgs_metadata = [sorter[i] for i in sorted(sorter)]
            imgs_metadata.extend(unrecognized)
    else:
        logger.info("Browse publish is not configured.")
        browse_urls = []
        imgs_metadata = []

    # set update json
    update_json = {
        "id": objectid,
        "objectid": objectid,
        "metadata": metadata,
        "dataset": ipath.split("/")[1],
        "ipath": ipath,
        "system_version": version,
        "dataset_level": level,
        "dataset_type": dtype,
        "urls": pub_urls,
        "browse_urls": browse_urls,
        "images": imgs_metadata,
        "prov": context.get("_prov", {}),
    }
    update_json.update(dataset)
    # logger.info("update_json: %s" % pformat(update_json))

    # custom index specified?
    index = r.getIndex()
    if index is not None:
        update_json["index"] = index

    # update GRQ
    if dry_run:
        update_json["grq_index_result"] = {"index": index}
        logger.info(
            "Would've indexed doc at %s: %s"
            % (grq_update_url, json.dumps(update_json, indent=2, sort_keys=True))
        )
    else:
        res = index_dataset(grq_update_url, update_json)
        logger.info("res: %s" % res)
        update_json["grq_index_result"] = res

    # finish if dry run
    if dry_run:
        try:
            shutil.rmtree(publ_ctx_dir)
        except:
            pass
        return prod_metrics, update_json

    # create PROV-ES JSON file for publish processStep
    prod_prov_es_file = os.path.join(
        local_prod_path, "%s.prov_es.json" % os.path.basename(local_prod_path)
    )
    pub_prov_es_bn = "publish.prov_es.json"
    if os.path.exists(prod_prov_es_file):
        pub_prov_es_file = os.path.join(local_prod_path, pub_prov_es_bn)
        prov_es_info = {}
        with open(prod_prov_es_file) as f:
            try:
                prov_es_info = json.load(f)
            except Exception as e:
                tb = traceback.format_exc()
                raise RuntimeError(
                    "Failed to load PROV-ES from {}: {}\n{}".format(
                        prod_prov_es_file, str(e), tb
                    )
                )
        log_publish_prov_es(
            prov_es_info,
            pub_prov_es_file,
            local_prod_path,
            pub_urls,
            prod_metrics,
            objectid,
        )
        # upload publish PROV-ES file
        osaka.main.put(
            pub_prov_es_file,
            os.path.join(pub_path_url, pub_prov_es_bn),
            params=osaka_params,
            noclobber=False,
        )

    # cleanup publish context
    if publ_ctx_url is not None:
        try:
            osaka.main.rmall(publ_ctx_url, params=osaka_params)
        except:
            logger.warn(
                "Failed to clean up publish context at {} on successful publish.".format(
                    publ_ctx_url
                )
            )
    try:
        shutil.rmtree(publ_ctx_dir)
    except:
        pass

    # queue data dataset
    queue_dataset(ipath, update_json, dataset_processed_queue)

    # return dataset metrics and dataset json
    return prod_metrics, update_json
