#!/usr/bin/env python3

import time
import logging
import sys
from typing import Union
try:
    import urllib3
    URLLIB3_INSTALLED = True
except ImportError:
    URLLIB3_INSTALLED = False

from os.path import dirname, realpath
sys.path.append(dirname(dirname(dirname(realpath(__file__)))))

from logger.utils.das_record import DASRecord  # noqa: E402
from logger.writers.writer import Writer  # noqa: E402

INFLUXDB_AUTH_TOKEN = INFLUXDB_ORG = INFLUXDB_URL = INFLUXDB_BUCKET = None
try:
    from database.influxdb.settings import INFLUXDB_AUTH_TOKEN, INFLUXDB_ORG  # noqa: E402
    from database.influxdb.settings import INFLUXDB_BUCKET  # noqa: E402
    from database.influxdb.settings import INFLUXDB_URL, INFLUXDB_VERIFY_SSL  # noqa: E402
    INFLUXDB_SETTINGS_FOUND = True
except (ModuleNotFoundError, ImportError):
    INFLUXDB_SETTINGS_FOUND = False
    INFLUXDB_VERIFY_SSL = False

try:
    from influxdb_client import InfluxDBClient  # noqa: E402
    from influxdb_client.client.write_api import ASYNCHRONOUS  # noqa: E402
    INFLUXDB_CLIENT_FOUND = True
except (ModuleNotFoundError, ImportError):
    INFLUXDB_CLIENT_FOUND = False


class InfluxDBWriter(Writer):
    """Write to the specified file. If filename is empty, write to stdout."""

    def __init__(self, bucket_name=INFLUXDB_BUCKET, measurement_name=None,
                 tags=None, auth_token=INFLUXDB_AUTH_TOKEN,
                 org=INFLUXDB_ORG, url=INFLUXDB_URL,
                 verify_ssl=INFLUXDB_VERIFY_SSL, quiet=False):
        """Write data records to the InfluxDB.
        ```
        bucket_name - the name of the bucket in InfluxDB.  If the bucket does
                  not exists then this writer will try to create it.

        measurement_name - optional measurement name to use. If not provided,
                  writer will use the record's data_id.

        tags - optional tags to be applied to records submitted to InfluxDB
               API.

               Example:
               tags:
                   tag0: value0
                   tag1:
                       value: value1
                       filter:
                           - measurement1
                           - measurement2
                       default: defaultValue1
                   tag2:
                       value: value2
                       filter: measurement2

        auth_token - The auth token required by the InfluxDB instance. If omitted,
                  will look for value in imported INFLUXDB_AUTH_TOKEN and throw
                  an exception if it is not found.

        org - The organization to associate with in the InfluxDB
                  instance. If omitted, will look for value in imported
                  INFLUXDB_ORG and throw an exception if it is not found.

        url - The URL at which to connect with the InfluxDB instance. If
                  omitted, will look for value in imported INFLUXDB_ORG
                  and throw an exception if it is not found.

        verify_ssl - If the URL begins with 'https', SSL will be used for the
                  connection. If so, and verify_ssl is true, the writer will
                  attempt to verify the validity of the relevant SSL certificate.
        ```
        """
        super().__init__(quiet=quiet)
        if not URLLIB3_INSTALLED:
            raise ImportError('InfluxDBWriter requires Python "urllib3" module; '
                              'please run "pip install urllib3"')
        if not auth_token:
            raise RuntimeError('No auth token specified in InfluxDBWriter and '
                               'none found in database/influxdb/settings.py. Have '
                               'you copied over database/influxdb/settings.py.dist '
                               'to database/influxdb/settings.py and followed the '
                               'configuration instructions in it?')
        if not org:
            raise RuntimeError('No organization specified in InfluxDBWriter and '
                               'none found in database/influxdb/settings.py. Have '
                               'you copied over database/influxdb/settings.py.dist '
                               'to database/influxdb/settings.py and followed the '
                               'configuration instructions in it?')
        if not url:
            raise RuntimeError('No URL specified in InfluxDBWriter and '
                               'none found in database/influxdb/settings.py. Have '
                               'you copied over database/influxdb/settings.py.dist '
                               'to database/influxdb/settings.py and followed the '
                               'configuration instructions in it?')

        if not INFLUXDB_SETTINGS_FOUND:
            raise RuntimeError('File database/influxdb/settings.py not found. '
                               'InfluxDB functionality is not available. Have '
                               'you copied over database/influxdb/settings.py.dist '
                               'to database/influxdb/settings.py and followed the '
                               'configuration instructions in it?')
        if not INFLUXDB_CLIENT_FOUND:
            raise RuntimeError('Python module influxdb_client not found. Please '
                               'install using "pip install influxdb_client" prior '
                               'to using InfluxDBWriter.')

        if tags and not isinstance(tags, dict):
            raise RuntimeError('The specified tags kwarg must be None or a dict')

        self.tags = {'*': {}}
        if tags:
            for tag, details in tags.items():
                if isinstance(details, str):
                    self.tags['*'][tag] = details

                if isinstance(details, dict) and 'filter' in details:
                    if isinstance(details['filter'], str):
                        details['filter'] = [details['filter']]

                    if 'default' in details:
                        self.tags['*'][tag] = details['default']

                    for filter_item in details['filter']:
                        if filter_item not in self.tags:
                            self.tags[filter_item] = {}

                        self.tags[filter_item][tag] = details['value']

        self.auth_token = auth_token
        self.org = org
        self.url = url
        self.use_ssl = url.find('https:') == 0
        self.verify_ssl = verify_ssl
        self.bucket_name = bucket_name
        self.measurement_name = measurement_name
        self.write_api = None

        # If we've chosen not to verify SSL, urllib3 will complain
        # mightily in the logs each time we make a call.
        urllib3.disable_warnings()

        # TODO: retry connecting if connection dies while writing.
        self._connect()

    ############################
    def _connect(self):

        while not self.write_api:
            client = InfluxDBClient(url=self.url, token=self.auth_token, org=self.org,
                                    ssl=self.use_ssl, verify_ssl=self.verify_ssl)  # type: ignore
            # get the orgID from the name:
            try:
                organizations_api = client.organizations_api()
                orgs = organizations_api.find_organizations()
            except BaseException:
                self.client = None
                logging.warning('Error connecting to the InfluxDB API. '
                                'Please confirm that InfluxDB is running and '
                                'that the authentication token is correct.'
                                'Sleeping before trying again.')
                time.sleep(5)
                continue

            # Look up the organization id for our org
            our_org = next((org for org in orgs if org.name == self.org), None)
            if not our_org:
                logging.fatal('Can not find org "%s" in InfluxDB', self.org)
                raise RuntimeError('Can not find org "%s" in InfluxDB' % self.org)
            self.org_id = our_org.id

            # get the bucketID from the name:
            bucket_api = client.buckets_api()
            bucket = bucket_api.find_bucket_by_name(self.bucket_name)

            # if the bucket does not exist then try to create it
            if bucket:
                self.bucket_id = bucket.id
            else:
                try:
                    logging.info('Creating new bucket for: %s', self.bucket_name)
                    new_bucket = bucket_api.create_bucket(bucket_name=self.bucket_name,
                                                          org_id=self.org_id)
                    self.bucket_id = new_bucket.id
                except BaseException:
                    logging.fatal('Can not create InfluxDB bucket "%s"', self.bucket_name)
                    raise RuntimeError('Can not create InfluxDB bucket "%s"'
                                       % self.bucket_name)

            self.write_api = client.write_api(write_options=ASYNCHRONOUS)

    ############################
    def write(self, record: Union[DASRecord, dict]):
        """Note: Assume record is a dict or DASRecord or list of
        dict/DASRecord. In each record look for 'fields', 'data_id' and
        'timestamp' (UTC epoch seconds). If data_id is missing, use the
        bucket_name we were initialized with.
        """

        def record_to_influx(record):
            """Put a single record into the format that InfluxDB wants."""
            if isinstance(record, DASRecord):
                data_id = record.data_id
                fields = record.fields
                timestamp = record.timestamp
            else:
                data_id = record.get('data_id')
                fields = record.get('fields', {})
                timestamp = record.get('timestamp') or time.time()

            measurement = self.measurement_name or data_id
            tags = {**{'sensor': measurement}, **self.tags['*']}

            if measurement in self.tags:
                tags = {**tags, **self.tags[measurement]}

            influxDB_record = {
                'measurement': self.measurement_name or data_id,
                'tags': tags,
                'fields': fields,
                'time': int(timestamp * 1000000000)
            }
            return influxDB_record

        # See if it's something we can process, and if not, try digesting
        if not self.can_process_record(record):  # inherited from BaseModule()
            self.digest_record(record)  # inherited from BaseModule()
            return

        try:
            logging.debug('InfluxDBWriter writing record: %s', record)
            influxDB_record = record_to_influx(record)
            # logging.info('influxdb\n bucket: %s\nrecord: %s',
            #             self.bucket_name, pprint.pformat(influxDB_record))
            self.write_api.write(self.bucket_id, self.org_id, influxDB_record)

        except Exception as e:
            if not self.quiet:
                logging.warning('InfluxDBWriter exception: %s', str(e))
                logging.warning('InfluxDBWriter could not ingest record '
                                'type %s: %s', type(record), str(record))
