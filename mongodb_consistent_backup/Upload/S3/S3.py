import os
import logging

from copy_reg import pickle
from math import ceil
from multiprocessing import Pool
from types import MethodType

from S3Session import S3Session
from S3UploadThread import S3UploadThread


# Allows pooled .apply_async()s to work on Class-methods:
def _reduce_method(m):
    if m.im_self is None:
        return getattr, (m.im_class, m.im_func.func_name)
    else:
        return getattr, (m.im_self, m.im_func.func_name)
pickle(MethodType, _reduce_method)


class S3:
    def __init__(self, config, source_dir, key_prefix):
        self.config          = config
        self.source_dir      = source_dir
        self.key_prefix      = key_prefix
        self.remove_uploaded = self.config.upload.remove_uploaded
        self.s3_host         = self.config.upload.s3.host
        self.bucket_name     = self.config.upload.s3.bucket_name
        self.bucket_prefix   = self.config.upload.s3.bucket_prefix
        self.access_key      = self.config.upload.s3.access_key
        self.secret_key      = self.config.upload.s3.secret_key
        self.thread_count    = self.config.upload.s3.threads
        self.chunk_size_mb   = self.config.upload.s3.chunk_size_mb
        self.chunk_size      = self.chunk_size_mb * 1024 * 1024

        self._pool        = None
        self._multipart   = None
        self._upload_done = False
        if None in (self.access_key, self.secret_key,self.s3_host):
            raise "Invalid S3 security key or host detected!"
        try:
            self.s3_conn = S3Session(self.access_key, self.secret_key, self.s3_host)
            self.bucket  = self.s3_conn.get_bucket(self.bucket_name)
        except Exception, e:
            raise e

    def run(self):
        if not os.path.isdir(self.source_dir):
            logging.error("The source directory: %s does not exist or is not a directory! Skipping AWS S3 Upload!" % self.source_dir)
        else:
            try:
                for file_name in os.listdir(self.source_dir):
                    if self.bucket_prefix == "/":
                        key_name = "/%s/%s" % (self.key_prefix, file_name)
                    else:
                        key_name = "%s/%s/%s" % (self.bucket_prefix, self.key_prefix, file_name)

                    file_path = "%s/%s" % (self.source_dir, file_name)
                    file_size = os.stat(file_path).st_size
                    chunk_count = int(ceil(file_size / float(self.chunk_size)))

                    logging.info("Starting multipart AWS S3 upload to key: %s%s using %i threads, %imb chunks, %i retries" % (
                        self.bucket_name,
                        key_name,
                        self.thread_count,
                        self.chunk_size_mb,
                        self.retries
                    ))
                    self._multipart = self.bucket.initiate_multipart_upload(key_name)
                    self._pool      = Pool(processes=self.thread_count)

                    for i in range(chunk_count):
                        offset = self.chunk_size * i
                        byte_count = min(self.chunk_size, file_size - offset)
                        part_num = i + 1
                        self._pool.apply_async(S3UploadThread(
                            self.bucket_name,
                            self.access_key,
                            self.secret_key,
                            self.s3_host,
                            self._multipart.id,
                            part_num,
                            file_path,
                            offset,
                            byte_count,
                            self.retries,
                            self.secure
                        ).run)
                    self._pool.close()
                    self._pool.join()
    
                    if len(self._multipart.get_all_parts()) == chunk_count:
                        self._multipart.complete_upload()
                        key = self.bucket.get_key(key_name)
                        key.set_acl(self.s3_acl)
                        self._upload_done = True

                        if self.remove_uploaded:
                            logging.info("Uploaded AWS S3 key: %s%s successfully. Removing local file" % (self.bucket_name, key_name))
                            os.remove("%s/%s" % (self.source_dir, file_name))
                        else:
                            logging.info("Uploaded AWS S3 key: %s%s successfully" % (self.bucket_name, key_name))
                    else:
                        self._multipart.cancel_upload()
                        logging.error("Failed to upload all multiparts for key: %s%s! Upload cancelled" % (self.bucket_name, key_name))
                        raise Exception, "Failed to upload all multiparts for key: %s%s! Upload cancelled" % (self.bucket_name, key_name), None

                if self.remove_uploaded:
                    logging.info("Removing backup source dir after successful AWS S3 upload of all backups")
                    os.rmdir(self.source_dir)
            except Exception, e:
                logging.error("Uploading to AWS S3 failed! Error: %s" % e)
                if self._multipart:
                    self._multipart.cancel_upload()
                raise e

    def close(self):
        if self._pool:
            logging.error("Terminating multipart AWS S3 upload threads")
            self._pool.terminate()
            self._pool.join()

        if self._multipart and not self._upload_done:
            logging.error("Cancelling incomplete multipart AWS S3 upload")
            self._multipart.cancel_upload()

        if self.s3_conn:
            self.s3_conn.close()
