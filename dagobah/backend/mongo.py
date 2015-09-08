""" Mongo Backend class built on top of base Backend """

from datetime import datetime
import re
import json

import pymongo
try:
    from pymongo import MongoClient
except ImportError:
    from pymongo import Connection

try:
    from pymongo.objectid import ObjectId
except ImportError:
    from bson.objectid import ObjectId

from dateutil import parser
from ..backend.base import BaseBackend

TRUNCATE_LOG_SIZES_CHAR = {'stdout': 5000000,
                           'stderr': 5000000}


class MongoBackend(BaseBackend):
    """ Mongo Backend implementation """
    required_packages = [{'pypi_name': 'pymongo',
                          'module_name': 'pymongo',
                          'version_key': 'version',
                          'version': '2.5'}]

    def __init__(self, host, port, db, dagobah_collection='dagobah',
                 job_collection='dagobah_job', log_collection='dagobah_log',
                 mongo_user=None, mongo_password=None):
        super(MongoBackend, self).__init__()

        self.host = host
        self.port = port
        self.db_name = db
        self.mongo_user = mongo_user
        self.mongo_password = mongo_password

        if self.mongo_user and self.mongo_password:
                self.client = MongoClient('mongodb://{}:{}@{}:{}/{}'.format(self.mongo_user, self.mongo_password,
                                                                            self.host, self.port, self.db_name))
        else:
            try:
                self.client = MongoClient(self.host, self.port)
            except NameError:
                self.client = Connection(self.host, self.port)

        self.db = self.client[self.db_name]

        self.dagobah_coll = self.db[dagobah_collection]
        self.job_coll = self.db[job_collection]
        self.log_coll = self.db[log_collection]

        self.log_coll.ensure_index("save_date")

    def __repr__(self):
        return '<MongoBackend (host: %s, port: %s)>' % (self.host, self.port)

    def get_known_dagobah_ids(self):
        results = []
        for rec in self.dagobah_coll.find():
            results.append(rec['_id'])
        return results

    def get_new_dagobah_id(self):
        while True:
            candidate = ObjectId()
            if not self.dagobah_coll.find_one({'_id': candidate}):
                return candidate

    def get_new_job_id(self):
        while True:
            candidate = ObjectId()
            if not self.job_coll.find_one({'_id': candidate}):
                return candidate

    def get_new_log_id(self):
        while True:
            candidate = ObjectId()
            if not self.log_coll.find_one({'_id': candidate}):
                return candidate

    def get_dagobah_json(self, dagobah_id):
        return self.dagobah_coll.find_one({'_id': dagobah_id})

    def decode_import_json(self, json_doc):
        def is_object_id(o):
            return (re.match(re.compile('^[0-9a-fA-f]{24}$'), o) is not None)
        transformers = [([is_object_id], ObjectId),
                        ([], parser.parse)]
        return super(MongoBackend, self).decode_import_json(json_doc,
                                                            transformers)

    def commit_dagobah(self, dagobah_json):
        dagobah_json['_id'] = dagobah_json['dagobah_id']
        append = {'save_date': datetime.utcnow()}
        self.dagobah_coll.save(dict(dagobah_json.items() + append.items()))

    def delete_dagobah(self, dagobah_id):
        """ Deletes the Dagobah and all child Jobs from the database.

        Related run logs are deleted as well.
        """

        rec = self.dagobah_coll.find_one({'_id': dagobah_id})
        for job in rec.get('jobs', []):
            if 'job_id' in job:
                self.delete_job(job['job_id'])
        self.log_coll.remove({'parent_id': dagobah_id})
        self.dagobah_coll.remove({'_id': dagobah_id})

    def commit_job(self, job_json):
        job_json['_id'] = job_json['job_id']
        append = {'save_date': datetime.utcnow()}
        self.job_coll.save(dict(job_json.items() + append.items()))

    def delete_job(self, job_id):
        self.job_coll.remove({'_id': job_id})

    def commit_log(self, log_json):
        """ Commits a run log to the Mongo backend.

        Due to limitations of maximum document size in Mongo,
        stdout and stderr logs are truncated to a maximum size for
        each task.
        """

        log_json['_id'] = log_json['log_id']
        append = {'save_date': datetime.utcnow()}

        for task_name, values in log_json.get('tasks', {}).items():
            for key, size in TRUNCATE_LOG_SIZES_CHAR.iteritems():
                if isinstance(values.get(key, None), str):
                    if len(values[key]) > size:
                        values[key] = '\n'.join([values[key][:size/2],
                                                 'DAGOBAH STREAM SPLIT',
                                                 values[key][-1 * (size/2):]])
        self.log_coll.save(dict(log_json.items() + append.items()))

        log_tasks = log_json['tasks']
        job_tasks = self.dagobah_coll.find_one({'jobs.job_id': log_json['job_id']})['jobs'][0]['tasks']
        for task in job_tasks:
            if task['name'] in log_tasks:
                log_task = log_tasks[task['name']]
                task['success'] = log_task.get('success')
                task['started_at'] = log_task.get('start_time')
                task['completed_at'] = log_task.get('complete_time')
                if log_task.get('start_time') and not log_task.get('complete_time'):
                    task['success'] = False
                    task['completed_at'] = log_task.get('start_time')

        self.dagobah_coll.find_one_and_update({'jobs.job_id': log_json['job_id']},
                                              {'$set': {'jobs.$.tasks': job_tasks}})

    def reset_task(self, job, task):
        graph = self.dagobah_coll.find_one({'jobs.job_id': job.job_id})
        tasks = [a for a in graph.get('jobs', []) if a['job_id'] == job.job_id][0]
        task_index = [i for i, a in enumerate(tasks['tasks']) if a['name'] == task.name][0]
        self.dagobah_coll.find_one_and_update({'jobs.job_id': job.job_id},
                                              {'$set': {'jobs.$.tasks.{}.completed_at'.format(task_index): None,
                                                        'jobs.$.tasks.{}.started_at'.format(task_index): None,
                                                        'jobs.$.tasks.{}.success'.format(task_index): None}})

    def get_latest_run_log(self, job_id, task_name):
        q = {'job_id': ObjectId(job_id),
             'tasks.%s' % task_name: {'$exists': True}}
        cur = self.log_coll.find(q).sort([('save_date', pymongo.DESCENDING)])
        for rec in cur:
            return rec
        return {}

    def get_run_log_history(self, job_id, task_name, limit=10):
        q = {'job_id': ObjectId(job_id),
             'tasks.%s' % task_name: {'$exists': True}}
        cur = self.log_coll.find(q).sort([('save_date',
                                           pymongo.DESCENDING)]).limit(limit)
        return list(cur)

    def get_run_log(self, job_id, task_name, log_id):
        q = {'job_id': ObjectId(job_id),
             'tasks.%s' % task_name: {'$exists': True},
             'log_id': ObjectId(log_id)}
        return self.log_coll.find_one(q)['tasks'][task_name]
