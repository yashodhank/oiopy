# Copyright (C) 2015 OpenIO SAS

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3.0 of the License, or (at your option) any later version.
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
# You should have received a copy of the GNU Lesser General Public
# License along with this library.


import hashlib
from functools import wraps
from cStringIO import StringIO
from urlparse import urlparse

import os
from eventlet import Timeout
from eventlet.greenpool import GreenPile
from eventlet.queue import Queue

from oiopy.api import API
from oiopy import exceptions
from oiopy import utils
from oiopy.exceptions import ConnectionTimeout, ChunkReadTimeout, \
    ChunkWriteTimeout
from oiopy.resource import Resource
from oiopy.service import Service
from oiopy.directory import DirectoryAPI
from oiopy.http import http_connect


CONTAINER_METADATA_PREFIX = "x-oio-container-meta-"
OBJECT_METADATA_PREFIX = "x-oio-content-meta-"

WRITE_CHUNK_SIZE = 65536
READ_CHUNK_SIZE = 65536

CONNECTION_TIMEOUT = 2
CHUNK_TIMEOUT = 3

PUT_QUEUE_DEPTH = 10

container_headers = {
    "size": "%ssys-m2-usage" % CONTAINER_METADATA_PREFIX,
    "ns": "%ssys-ns" % CONTAINER_METADATA_PREFIX
}

object_headers = {
    "name": "%sname" % OBJECT_METADATA_PREFIX,
    "policy": "%spolicy" % OBJECT_METADATA_PREFIX,
    "version": "%sversion" % OBJECT_METADATA_PREFIX,
    "content_type": "%smime-type" % OBJECT_METADATA_PREFIX,
    "size": "%slength" % OBJECT_METADATA_PREFIX,
    "ctime": "%sctime" % OBJECT_METADATA_PREFIX,
    "hash": "%shash" % OBJECT_METADATA_PREFIX,
    "chunk_method": "%schunk-method" % OBJECT_METADATA_PREFIX
}


def ensure_container(fnc):
    @wraps(fnc)
    def _wrapped(self, account, container, *args, **kwargs):
        if not isinstance(container, Container):
            container = self._make(account, container)
        return fnc(self, account, container, *args, **kwargs)

    return _wrapped


def handle_container_not_found(fnc):
    @wraps(fnc)
    def _wrapped(self, account, container, *args, **kwargs):
        try:
            return fnc(self, account, container, *args, **kwargs)
        except exceptions.NotFound as e:
            name = utils.get_id(container)
            e.message = "Container '%s' does not exist." % name
            raise exceptions.NoSuchContainer(e)

    return _wrapped


def handle_object_not_found(fnc):
    @wraps(fnc)
    def _wrapped(self, obj, *args, **kwargs):
        try:
            return fnc(self, obj, *args, **kwargs)
        except exceptions.NotFound as e:
            name = utils.get_id(obj)
            e.message = "Object '%s' does not exist." % name
            raise exceptions.NoSuchObject(e)

    return _wrapped


class StorageAPI(API):
    """
    The storage API
    """

    def __init__(self, endpoint_url, namespace):
        super(StorageAPI, self).__init__(endpoint_url)
        directory = DirectoryAPI(endpoint_url, namespace)
        self.namespace = namespace
        self._service = ContainerService(self, directory=directory)
        self._account_url = None

    def get_account(self, account, headers=None):
        uri = "/v1.0/account/show"
        account_id = utils.quote(account, '')
        uri = "%s?id=%s" % (uri, account_id)
        resp, resp_body = self._account_request(uri, 'GET', headers=headers)
        return resp_body

    def list_containers(self, account, limit=None, marker=None, prefix=None,
                        delimiter=None, end_marker=None, headers=None):
        uri = "/v1.0/account/containers"
        account_id = utils.quote(account, '')
        d = {"id": account_id, "limit": limit, "marker": marker,
             "delimiter": delimiter, "prefix": prefix, "end_marker": end_marker}
        query_string = "&".join(["%s=%s" % (k, v) for k, v in d.iteritems()
                                 if v is not None])
        if query_string:
            uri = "%s?%s" % (uri, query_string)

        resp, resp_body = self._account_request(uri, 'GET', headers=headers)
        listing = resp_body['listing']

        containers = []
        for c in listing:
            container = self._service._make(account, c[0])
            data = {"total_size": c[2],
                    "total_objects": c[1],
                    "is_subdir": bool(c[3])}
            container._add_data(data)
            containers.append(container)

        del resp_body['listing']
        return containers, resp_body

    def list_container_objects(self, account, container, limit=None,
                               marker=None, prefix=None, delimiter=None,
                               end_marker=None, headers=None):
        """
        Return the list of objects in the specified container.
        """
        return self._service.list(account, container, limit=limit,
                                  marker=marker, prefix=prefix,
                                  delimiter=delimiter, end_marker=end_marker,
                                  headers=headers)

    def get_container_metadata(self, account, container, headers=None):
        """
        Return the metadata for the specified container.
        """
        return self._service.get_metadata(account, container, headers=headers)

    def set_container_metadata(self, account, container, metadata,
                               headers=None):
        """
        Update the metadata for the specified container.
        """
        return self._service.set_metadata(account, container, metadata,
                                          headers=headers)

    def delete_container_metadata(self, account, container, keys, headers=None):
        """
        Delete metadata for the specified container.
        """
        return self._service.delete_metadata(account, container, keys,
                                             headers=headers)

    def get_object_metadata(self, account, container, obj, headers=None):
        """
        Return the metadata for the specified object.
        """
        return self._service.get_object_metadata(account, container, obj,
                                                 headers=headers)

    def set_object_metadata(self, account, container, obj, metadata,
                            clear=False,
                            headers=None):
        """
        Update the metadata for the specified object.
        """
        return self._service.set_object_metadata(account, container, obj,
                                                 metadata, clear=clear,
                                                 headers=headers)

    def delete_object_metadata(self, account, container, obj, keys,
                               headers=None):
        """
        Delete metadata for the specified object.
        """
        return self._service.delete_object_metadata(account, container, obj,
                                                    keys, headers=headers)

    def get_object(self, account, container, obj, headers=None):
        """
        Return the specified object.
        """
        return self._service.get_object(account, container, obj,
                                        headers=headers)

    def upload_file(self, account, container, file_or_path, obj_name=None,
                    etag=None, content_type=None, content_length=None,
                    metadata=None, headers=None, return_none=False):
        """
        Upload the file in the specified container and return the new object.
        A file path or a file-like object must be given.

        If no obj_name is given, the file's name will be used.

        """
        return self.create_object(account, container, file_or_path=file_or_path,
                                  obj_name=obj_name, content_type=content_type,
                                  content_length=content_length, etag=etag,
                                  metadata=metadata, headers=headers,
                                  return_none=return_none)

    def store_object(self, account, container, obj_name, data,
                     content_type=None,
                     etag=None, content_length=None, headers=None,
                     metadata=None, return_none=False):
        """
        Store a new object in the specified container and return the created
        object.
        """
        return self.create_object(account, container, obj_name=obj_name,
                                  data=data, content_type=content_type,
                                  etag=etag, content_length=content_length,
                                  metadata=metadata, headers=headers,
                                  return_none=return_none)

    def create_object(self, account, container, file_or_path=None, data=None,
                      etag=None, obj_name=None, content_length=None,
                      content_type=None, metadata=None, headers=None,
                      return_none=False):
        """
        Create an object in the specified container.
        """
        return self._service.create_object(account, container,
                                           file_or_path=file_or_path,
                                           obj_name=obj_name, data=data,
                                           content_length=content_length,
                                           content_type=content_type, etag=etag,
                                           metadata=metadata, headers=headers,
                                           return_none=return_none)

    def fetch_object(self, account, container, obj, size=None, offset=0,
                     with_meta=False, headers=None):
        """
        Fetch the object from the specified container.
        """
        return self._service.fetch_object(account, container, obj, size=size,
                                          offset=offset, with_meta=with_meta,
                                          headers=headers)

    def delete(self, account, container, headers=None):
        """
        Delete the specified container.
        """
        return self._service.delete(account, container, headers=headers)

    def delete_object(self, account, container, obj, headers=None):
        """
        Delete the object from the specified container.
        """
        return self._service.delete_object(account, container, obj,
                                           headers=headers)

    def _account_request(self, uri, method, **kwargs):
        account_url = self.get_account_url()
        uri = "%s%s" % (account_url, uri)
        resp, resp_body = self._request(uri, method, **kwargs)
        return resp, resp_body

    def get_account_url(self):
        uri = '/lb/%s/account' % self.namespace
        resp, resp_body = self._request(uri, 'GET')
        if resp.status_code == 200:
            instance_info = resp_body[0]
            return 'http://%s/' % instance_info['addr']
        else:
            raise exceptions.ClientException("could not find account instance "
                                             "url")


class StorageObject(Resource):
    """
    Storage Object Resource
    """

    def __repr__(self):
        return "<Object '%s'>" % self.name

    @property
    def id(self):
        return self.name

    def fetch(self, size=None, offset=0, with_meta=False, headers=None):
        return self.service.fetch(self, size=size, offset=offset,
                                  with_meta=with_meta, headers=headers)

    def get_metadata(self, headers=None):
        return self.service.get_metadata(self, headers=headers)

    def set_metadata(self, metadata, clear=False, headers=None):
        return self.service.set_metadata(self, metadata, clear=clear,
                                         headers=headers)

    def delete_metadata(self, keys, headers=None):
        return self.service.delete_metadata(self, keys, headers=headers)

    def copy(self, destination, headers=None):
        return self.service.copy(self, destination, headers=headers)


class Container(Resource):
    def __init__(self, *args, **kwargs):
        super(Container, self).__init__(*args, **kwargs)
        self.uri_base = "%s/%s/%s" % (self.service.uri_base, utils.quote(
            self.account, ''), utils.quote(self.id, ''))
        self.object_service = StorageObjectService(self.service.api,
                                                   uri_base=self.uri_base,
                                                   resource_class=StorageObject)

    def __repr__(self):
        return "<Container '%s'>" % self.name

    def reload(self):
        n = self.service.get(self.account, self)
        if n:
            self._add_data(n._data)

    @property
    def id(self):
        return self.name

    def get_metadata(self, headers=None):
        return self.service.get_metadata(self.account, self, headers=headers)

    def set_metadata(self, metadata, clear=False, headers=None):
        return self.service.set_metadata(self.account, self, metadata,
                                         clear=clear,
                                         headers=headers)

    def delete_metadata(self, keys, headers=None):
        return self.service.delete_metadata(self.account, self, keys,
                                            headers=headers)

    def get_object(self, obj_name, headers=None):
        return self.object_service.get(obj_name, headers=headers)

    def fetch(self, obj, size=None, offset=0, with_meta=False, headers=None):
        return self.object_service.fetch(obj, size=size, offset=offset,
                                         with_meta=with_meta, headers=headers)

    def list(self, marker=None, limit=None, prefix=None,
             delimiter=None, end_marker=None, headers=None):
        return self.service.list(self.account, self, marker=marker, limit=limit,
                                 prefix=prefix, delimiter=delimiter,
                                 end_marker=end_marker, headers=headers)

    def create(self, file_or_path=None, data=None, obj_name=None,
               content_type=None, etag=None, content_encoding=None,
               content_length=None, metadata=None, headers=None,
               return_none=False):
        return self.object_service.create(file_or_path=file_or_path, data=data,
                                          obj_name=obj_name,
                                          content_type=content_type, etag=etag,
                                          content_encoding=content_encoding,
                                          content_length=content_length,
                                          metadata=metadata, headers=headers,
                                          return_none=return_none)

    def delete_object(self, obj, headers=None):
        return self.object_service.delete(obj, headers=headers)

    def delete(self, del_objects=False, headers=None):
        return self.service.delete(self.account, self, del_objects=del_objects,
                                   headers=headers)

    def get_object_metadata(self, obj, headers=None):
        return self.object_service.get_metadata(obj, headers=headers)

    def set_object_metadata(self, obj, metadata, clear=False, headers=None):
        return self.object_service.set_metadata(obj, metadata, clear=clear,
                                                headers=headers)

    def delete_object_metadata(self, obj, keys, headers=None):
        return self.object_service.delete_metadata(obj, keys, headers=headers)


class ContainerService(Service):
    def __init__(self, api, directory):
        uri_base = '/m2/%s' % api.namespace
        super(ContainerService, self).__init__(api, uri_base=uri_base)
        self.directory = directory

    def _make(self, account, name):
        data = {"name": name, "account": account}
        return Container(self, data)

    def _make_uri(self, account, id_or_obj):
        obj_id = utils.get_id(id_or_obj)
        return "%s/%s/%s" % (self.uri_base, account, utils.quote(obj_id, ''))

    @handle_container_not_found
    def get(self, account, container, headers=None):
        """
        Get a specific container.
        """
        uri = self._make_uri(account, container)
        resp, resp_body = self.api.do_head(uri, headers=headers)
        headers = resp.headers
        total_size = int(
            headers.get(container_headers["size"], "0"))
        ns = headers.get(container_headers["ns"])
        name = utils.get_id(container)
        data = {"name": name,
                "account": account,
                "total_size": total_size,
                "namespace": ns}
        return Container(self, data)

    def create(self, account, name, metadata=None, return_none=False,
               headers=None):
        """
        Create a new container.
        """
        try:
            self.directory.link(account, name, "meta2", headers=headers)
        except exceptions.NotFound:
            self.directory.create(account, name, True, headers=headers)
            self.directory.link(account, name, "meta2", headers=headers)

        uri = self._make_uri(account, name)

        resp, resp_body = self.api.do_put(uri, headers=headers)
        if resp.status_code in (204, 201):
            if not return_none:
                return self.get(account, name)
        else:
            raise exceptions.from_response(resp, resp_body)

    @handle_container_not_found
    def delete(self, account, container, del_objects=False, headers=None):
        """
        Delete the specified container.
        """
        if del_objects:
            pass

        uri = self._make_uri(account, container)
        try:
            resp, resp_body = self.api.do_delete(uri, headers=headers)
        except exceptions.Conflict as e:
            raise exceptions.ContainerNotEmpty(e)

        self.directory.unlink(account, container, "meta2", headers=headers)

    @handle_container_not_found
    def get_metadata(self, account, container, headers=None):
        uri = self._make_uri(account, container)
        resp, resp_body = self._action(uri, 'GetProperties', None,
                                       headers=headers)
        return resp_body

    @handle_container_not_found
    def set_metadata(self, account, container, metadata, clear=False,
                     headers=None):
        uri = self._make_uri(account, container)
        if clear:
            uri += '?flush=1'
        resp, resp_body = self._action(uri, 'SetProperties', metadata,
                                       headers=headers)

    @handle_container_not_found
    def delete_metadata(self, account, container, keys, headers=None):
        uri = self._make_uri(account, container)
        resp, resp_body = self._action(uri, 'DelProperties', keys,
                                       headers=headers)


    @ensure_container
    def create_object(self, account, container, file_or_path=None, data=None,
                      etag=None,
                      obj_name=None, content_type=None, content_length=None,
                      metadata=None, headers=None, return_none=False):

        return container.create(file_or_path=file_or_path, data=data, etag=etag,
                                obj_name=obj_name, content_type=content_type,
                                content_length=content_length,
                                metadata=metadata, headers=headers,
                                return_none=return_none)

    @ensure_container
    def delete_object(self, account, container, obj, headers=None):

        return container.delete_object(obj, headers=headers)

    @handle_container_not_found
    def set_storage_policy(self, account, container, storage_policy,
                           headers=None):
        uri = self._make_uri(account, container)
        self._action(uri, "SetStoragePolicy", storage_policy, headers=headers)

    @handle_container_not_found
    def list(self, account, container, limit=None, marker=None, prefix=None,
             delimiter=None, end_marker=None, headers=None):
        uri = self._make_uri(account, container)
        d = {"max": limit, "marker": marker, "delimiter": delimiter,
             "prefix": prefix, "end_marker": end_marker}
        query_string = "&".join(["%s=%s" % (k, v) for k, v in d.iteritems()
                                 if v is not None])

        if query_string:
            uri = "%s?%s" % (uri, query_string)
        resp, resp_body = self.api.do_get(uri, headers=headers)

        container = self._make(account, utils.get_id(container))

        objects = [StorageObject(container.object_service, el) for el in
                   resp_body["objects"]]
        return objects

    @ensure_container
    def fetch_object(self, account, container, obj, size=None, offset=0,
                     with_meta=False, headers=None):

        return container.fetch(obj, size=size, offset=offset,
                               with_meta=with_meta, headers=headers)

    @ensure_container
    def set_object_metadata(self, account, container, obj, metadata,
                            clear=False, headers=None):

        return container.set_object_metadata(obj, metadata, clear=clear,
                                             headers=headers)

    @ensure_container
    def get_object_metadata(self, account, container, obj, headers=None):

        return container.get_object_metadata(obj, headers=headers)

    @ensure_container
    def delete_object_metadata(self, account, container, obj, keys,
                               headers=None):

        return container.delete_object_metadata(obj, keys, headers=headers)

    @ensure_container
    def get_object(self, account, container, obj, headers=None):

        return container.get_object(obj, headers=headers)


class StorageObjectService(Service):
    def _make_uri(self, obj_or_id):
        obj_id = utils.get_id(obj_or_id)
        return "%s/%s" % (self.uri_base, utils.quote(obj_id, ''))

    @handle_object_not_found
    def get(self, obj, headers=None):
        """
        Get the metadata of the specified object.
        """
        uri = self._make_uri(obj)
        resp, resp_body = self.api.do_head(uri, headers=headers)
        headers = resp.headers

        return self._make_storage_object(headers)

    def _make_storage_object(self, headers):
        try:
            size = int(headers.get(object_headers["size"]))
        except (TypeError, ValueError):
            size = None

        try:
            version = int(headers.get(object_headers["version"]))
        except (TypeError, ValueError):
            version = None
        name = headers.get(object_headers["name"])
        content_hash = headers.get(object_headers["hash"])
        policy = headers.get(object_headers["policy"])
        content_type = headers.get(object_headers["content_type"])
        data = {"name": name,
                "size": size,
                "content_type": content_type,
                "hash": content_hash,
                "version": version,
                "policy": policy}
        return StorageObject(self, data)

    def create(self, file_or_path=None, data=None, obj_name=None,
               content_type=None, etag=None, content_encoding=None,
               content_length=None, metadata=None, headers=None,
               return_none=False):

        if (data, file_or_path) == (None, None):
            raise exceptions.MissingData()
        src = data if data is not None else file_or_path
        if src is file_or_path:
            if isinstance(file_or_path, basestring):
                if not os.path.exists(file_or_path):
                    raise exceptions.FileNotFound("File '%s' not found." %
                                                  file_or_path)
                file_name = os.path.basename(file_or_path)
            else:
                try:
                    file_name = os.path.basename(file_or_path.name)
                except AttributeError:
                    file_name = None
            obj_name = obj_name or file_name
        if not obj_name:
            raise exceptions.MissingName("No name for the object has been "
                                         "specified")

        if isinstance(data, basestring):
            content_length = len(data)

        if content_length is None:
            raise exceptions.MissingContentLength()

        sysmeta = {'content_type': content_type,
                   'content_encoding': content_encoding,
                   'content_length': content_length,
                   'etag': etag}

        if src is data:
            self._create(obj_name, StringIO(data), sysmeta,
                         headers=headers)
        elif hasattr(file_or_path, "read"):
            self._create(obj_name, src, sysmeta, headers=headers)
        else:
            with open(file_or_path, "rb") as f:
                self._create(obj_name, f, sysmeta, headers=headers)
        if not return_none:
            return self.get(obj_name, headers=headers)

    def _create(self, obj_name, src, sysmeta, headers=None):
        uri = self._make_uri(obj_name)
        args = {"size": sysmeta['content_length']}
        resp, resp_body = self._action(uri, "Beans", args, headers=headers)

        raw_chunks = resp_body

        rain_security = len(raw_chunks[0]["pos"].split(".")) == 2
        if rain_security:
            raise exceptions.OioException('RAIN Security not supported.')

        chunks = self._sort_chunks(raw_chunks, rain_security)
        final_chunks, bytes_transferred, content_checksum = self._put_stream(
            obj_name, src, sysmeta, chunks, headers=headers)

        sysmeta['etag'] = content_checksum
        self._put_object(obj_name, bytes_transferred, sysmeta,
                         final_chunks)

    def _put_stream(self, obj_name, src, sysmeta, chunks, headers=None):
        global_checksum = hashlib.md5()
        total_bytes_transferred = 0
        content_chunks = []

        def _connect_put(chunk):
            raw_url = chunk["url"]
            parsed = urlparse(raw_url)
            try:
                chunk_path = parsed.path.split('/')[-1]
                headers = {}
                headers["transfer-encoding"] = "chunked"
                headers["content_path"] = utils.quote(obj_name)
                headers["content_size"] = sysmeta['content_length']
                headers["content_chunksnb"] = len(chunks)
                headers["chunk_position"] = chunk["pos"]
                headers["chunk_id"] = chunk_path

                with ConnectionTimeout(CONNECTION_TIMEOUT):
                    conn = http_connect(parsed.netloc, 'PUT', parsed.path,
                                        headers)
                    conn.chunk = chunk
                return conn
            except (Exception, Timeout) as e:
                pass

        def _send_data(conn):
            while True:
                data = conn.queue.get()
                if not conn.failed:
                    try:
                        with ChunkWriteTimeout(CHUNK_TIMEOUT):
                            conn.send(data)
                    except (Exception, ChunkWriteTimeout):
                        conn.failed = True
                conn.queue.task_done()

        for pos in range(len(chunks)):
            current_chunks = chunks[pos]

            pile = GreenPile(len(current_chunks))

            for current_chunk in current_chunks:
                pile.spawn(_connect_put, current_chunk)

            conns = [conn for conn in pile if conn]

            min_conns = 1

            if len(conns) < min_conns:
                raise exceptions.OioException("RAWX connection failure")

            bytes_transferred = 0
            total_size = current_chunks[0]["size"]
            chunk_checksum = hashlib.md5()
            try:
                with utils.ContextPool(len(current_chunks)) as pool:
                    for conn in conns:
                        conn.failed = False
                        conn.queue = Queue(PUT_QUEUE_DEPTH)
                        pool.spawn(_send_data, conn)

                    while True:
                        remaining_bytes = total_size - bytes_transferred
                        if WRITE_CHUNK_SIZE < remaining_bytes:
                            read_size = WRITE_CHUNK_SIZE
                        else:
                            read_size = remaining_bytes
                        with ChunkReadTimeout(CHUNK_TIMEOUT):
                            data = src.read(read_size)
                            if len(data) == 0:
                                for conn in conns:
                                    conn.queue.put('0\r\n\r\n')
                                break
                        chunk_checksum.update(data)
                        global_checksum.update(data)
                        bytes_transferred += len(data)
                        for conn in conns:
                            if not conn.failed:
                                conn.queue.put('%x\r\n%s\r\n' % (len(data),
                                                                 data))
                            else:
                                conns.remove(conn)

                        if len(conns) < min_conns:
                            raise exceptions.OioException("RAWX write failure")

                    for conn in conns:
                        if conn.queue.unfinished_tasks:
                            conn.queue.join()

                conns = [conn for conn in conns if not conn.failed]

            except ChunkReadTimeout:
                raise exceptions.ClientReadTimeout()
            except (Exception, Timeout):
                raise exceptions.OioException("Exception during chunk "
                                              "write.")

            final_chunks = []
            for conn in conns:
                resp = conn.getresponse()
                if resp.status in (200, 201):
                    conn.chunk["size"] = bytes_transferred
                    final_chunks.append(conn.chunk)
                conn.close()
            if len(final_chunks) < min_conns:
                raise exceptions.OioException("RAWX write failure")

            checksum = chunk_checksum.hexdigest()
            for chunk in final_chunks:
                chunk["hash"] = checksum
            content_chunks += final_chunks
            total_bytes_transferred += bytes_transferred

        content_checksum = global_checksum.hexdigest()

        return content_chunks, total_bytes_transferred, content_checksum

    def _put_object(self, obj_name, content_length, sysmeta, chunks,
                    headers=None):

        headers = {"x-oio-content-meta-length": content_length,
                   "x-oio-content-meta-hash": sysmeta['etag'],
                   "content-type": sysmeta['content_type']}

        uri = self._make_uri(obj_name)
        resp, resp_body = self.api.do_put(uri, headers=headers,
                                          body=chunks)

    @handle_object_not_found
    def fetch(self, obj, size=None, offset=0, with_meta=False, headers=None):
        """
        Fetch the object data.
        """
        uri = self._make_uri(obj)

        resp, resp_body = self.api.do_get(uri, headers=headers)

        meta = self._make_metadata(resp.headers)
        raw_chunks = resp_body

        rain_security = len(raw_chunks[0]["pos"].split(".")) == 2
        chunks = self._sort_chunks(raw_chunks, rain_security)
        stream = self._fetch_stream(meta, chunks, rain_security, size, offset,
                                    headers=headers)

        if with_meta:
            return meta, stream
        return stream

    def _sort_chunks(self, raw_chunks, rain_security):
        chunks = dict()
        for chunk in raw_chunks:
            raw_position = chunk["pos"].split(".")
            position = int(raw_position[0])
            if rain_security:
                subposition = raw_position[1]
            if position in chunks:
                if rain_security:
                    chunks[position][subposition] = chunk
                else:
                    chunks[position].append(chunk)
            else:
                if rain_security:
                    chunks[position] = dict()
                    chunks[position][subposition] = chunk
                else:
                    chunks[position] = [chunk]
        return chunks

    def _fetch_stream(self, meta, chunks, rain_security, size, offset,
                      headers=None):
        current_offset = 0
        total_bytes = 0
        if size is None:
            size = int(meta["length"])
        if rain_security:
            raise exceptions.OioException("RAIN not supported")
        else:
            for pos in range(len(chunks)):
                chunk_size = int(chunks[pos][0]["size"])
                if total_bytes >= size:
                    break
                if current_offset + chunk_size > offset:
                    if current_offset < offset:
                        _offset = offset - current_offset
                    else:
                        _offset = 0
                    if chunk_size + total_bytes > size:
                        _size = size - total_bytes
                    else:
                        _size = chunk_size

                    handler = ChunkDownloadHandler(chunks[pos], _size, _offset)
                    stream = handler.get_stream()
                    if not stream:
                        raise exceptions.OioException("Error while downloading")
                    for s in stream:
                        total_bytes += len(s)
                        yield s
                current_offset += chunk_size

    @handle_object_not_found
    def delete(self, obj, headers=None):
        uri = self._make_uri(obj)
        resp, resp_body = self.api.do_delete(uri, headers=headers)

    @handle_object_not_found
    def get_metadata(self, obj, prefix=None, headers=None):
        uri = self._make_uri(obj)
        resp, resp_body = self._action(uri, 'GetProperties',
                                       None, headers=headers)

        meta = self._make_metadata(resp.headers, prefix=prefix)
        for k, v in resp_body.iteritems():
            meta[k] = v
        return meta

    def _make_metadata(self, headers, prefix=None):
        meta = {}
        if prefix is None:
            prefix = OBJECT_METADATA_PREFIX

        for k, v in headers.iteritems():
            k = k.lower()
            if k.startswith(prefix):
                key = k.replace(prefix, "")
                meta[key] = v
        return meta

    @handle_object_not_found
    def set_metadata(self, obj, metadata, clear=False, headers=None):
        uri = self._make_uri(obj)
        if clear:
            self.delete_metadata(obj, [], headers=headers)
        resp, resp_body = self._action(uri, "SetProperties", metadata, headers)

    @handle_object_not_found
    def delete_metadata(self, obj, keys, headers=None):
        uri = self._make_uri(obj)
        resp, resp_body = self._action(uri, "DelProperties", keys, headers)


class ChunkDownloadHandler(object):
    def __init__(self, chunks, size, offset, headers=None):
        self.chunks = chunks
        self.failed_chunks = []

        headers = {}
        h_range = "bytes=%d-" % offset
        end = None
        if size >= 0:
            end = (size + offset - 1)
            h_range += str(end)
        headers["Range"] = h_range
        self.headers = headers
        self.begin = offset
        self.end = end

    def get_stream(self):
        source = self._get_chunk_source()
        stream = None
        if source:
            stream = self._make_stream(source)
        return stream

    def _fast_forward(self, nb_bytes):
        self.begin += nb_bytes
        if self.end and self.begin > self.end:
            raise Exception('Requested Range Not Satisfiable')
        h_range = 'bytes=%d-' % self.begin
        if self.end:
            h_range += str(self.end)
        self.headers['Range'] = h_range

    def _get_chunk_source(self):
        source = None
        for chunk in self.chunks:
            try:
                with ConnectionTimeout(CONNECTION_TIMEOUT):
                    raw_url = chunk["url"]
                    parsed = urlparse(raw_url)
                    conn = http_connect(parsed.netloc, 'GET', parsed.path,
                                        self.headers)
                source = conn.getresponse(True)
                source.conn = conn

            except Exception as e:
                self.failed_chunks.append(chunk)
                continue
            if source.status not in (200, 206):
                self.failed_chunks.append(chunk)
                source.conn.close()
                source = None
            else:
                break

        return source

    def _make_stream(self, source):
        bytes_read = 0
        try:
            while True:
                try:
                    data = source.read(READ_CHUNK_SIZE)
                    bytes_read += len(data)
                except ChunkReadTimeout:
                    self._fast_forward(bytes_read)
                    new_source = self._get_chunk_source()
                    if new_source:
                        source.conn.close()
                        source = new_source
                        bytes_read = 0
                        continue
                    else:
                        raise
                if not data:
                    break
                yield data
        except ChunkReadTimeout:
            # error while reading chunk
            raise
        except GeneratorExit:
            # client premature stop
            pass
        except Exception:
            # error
            raise
        finally:
            source.conn.close()
