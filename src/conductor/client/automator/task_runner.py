import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from conductor.client.configuration.configuration import Configuration
from conductor.client.configuration.settings.metrics_settings import MetricsSettings
from conductor.client.http.api.task_resource_api import TaskResourceApi
from conductor.client.http.api_client import ApiClient
from conductor.client.http.models.task import Task
from conductor.client.http.models.task_exec_log import TaskExecLog
from conductor.client.http.models.task_result import TaskResult
from conductor.client.http.rest import AuthorizationException
from conductor.client.telemetry.metrics_collector import MetricsCollector
from conductor.client.worker.worker_interface import WorkerInterface

logger = logging.getLogger(
    Configuration.get_logging_formatted_name(
        __name__
    )
)


class TaskRunner:
    def __init__(
            self,
            worker: WorkerInterface,
            configuration: Configuration = None,
            metrics_settings: MetricsSettings = None
    ):
        if not isinstance(worker, WorkerInterface):
            raise Exception('Invalid worker')
        self.worker = worker
        self.__set_worker_properties()
        if not isinstance(configuration, Configuration):
            configuration = Configuration()
        self.configuration = configuration
        self.metrics_collector = None
        if metrics_settings is not None:
            self.metrics_collector = MetricsCollector(
                metrics_settings
            )
        self.task_client = TaskResourceApi(
            ApiClient(
                configuration=self.configuration
            )
        )

    def run(self) -> None:
        if self.configuration is not None:
            self.configuration.apply_logging_config()
        else:
            logger.setLevel(logging.DEBUG)

        task_names = ','.join(self.worker.task_definition_names)
        logger.info(f'Polling task {task_names} with domain {self.worker.get_domain()} with polling '
                    f'interval {self.worker.get_polling_interval_in_seconds()}')

        while True:
            try:
                self.run_once()
            except Exception as e:
                pass

    def __execute_and_update_task(self, task):
        task_result = self.__execute_task(task)
        self.__update_task(task_result)

    def run_once(self) -> None:
        task = self.__poll_task()
        if task is not None and task.task_id is not None:
            # with ThreadPoolExecutor(max_workers=self.execution_threads) as executor:
            with ThreadPoolExecutor(max_workers=configuration.execution_threads) as executor:
                # Submit the task execution and update as a single operation
                future = executor.submit(self.__execute_and_update_task, task)
        self.__wait_for_polling_interval()
        self.worker.clear_task_definition_name_cache()

    def __poll_task(self) -> Task:
        task_definition_name = self.worker.get_task_definition_name()
        if self.worker.paused():
            logger.debug(f'Stop polling task for: {task_definition_name}')
            return None
        if self.metrics_collector is not None:
            self.metrics_collector.increment_task_poll(
                task_definition_name
            )

        try:
            start_time = time.time()
            domain = self.worker.get_domain()
            params = {'workerid': self.worker.get_identity()}
            if domain is not None:
                params['domain'] = domain
            task = self.task_client.poll(tasktype=task_definition_name, **params)
            finish_time = time.time()
            time_spent = finish_time - start_time
            if self.metrics_collector is not None:
                self.metrics_collector.record_task_poll_time(task_definition_name, time_spent)
        except AuthorizationException as auth_exception:
            if self.metrics_collector is not None:
                self.metrics_collector.increment_task_poll_error(task_definition_name, type(auth_exception))
            if auth_exception.invalid_token:
                logger.fatal(f'failed to poll task {task_definition_name} due to invalid auth token')
            else:
                logger.fatal(f'failed to poll task {task_definition_name} error: {auth_exception.status} - {auth_exception.error_code}')
            return None
        except Exception as e:
            if self.metrics_collector is not None:
                self.metrics_collector.increment_task_poll_error(task_definition_name, type(e))
            logger.error(
                f'Failed to poll task for: {task_definition_name}, reason: {traceback.format_exc()}'
            )
            return None
        if task is not None:
            logger.debug(
                f'Polled task: {task_definition_name}, worker_id: {self.worker.get_identity()}, domain: {self.worker.get_domain()}')
        return task

    def __execute_task(self, task: Task) -> TaskResult:
        if not isinstance(task, Task):
            return None
        task_definition_name = self.worker.get_task_definition_name()
        logger.debug(
            'Executing task, id: {task_id}, workflow_instance_id: {workflow_instance_id}, task_definition_name: {task_definition_name}'.format(
                task_id=task.task_id,
                workflow_instance_id=task.workflow_instance_id,
                task_definition_name=task_definition_name
            )
        )
        try:
            start_time = time.time()
            task_result = self.worker.execute(task)
            finish_time = time.time()
            time_spent = finish_time - start_time
            if self.metrics_collector is not None:
                self.metrics_collector.record_task_execute_time(
                    task_definition_name,
                    time_spent
                )
                self.metrics_collector.record_task_result_payload_size(
                    task_definition_name,
                    sys.getsizeof(task_result)
                )
            logger.debug(
                'Executed task, id: {task_id}, workflow_instance_id: {workflow_instance_id}, task_definition_name: {task_definition_name}'.format(
                    task_id=task.task_id,
                    workflow_instance_id=task.workflow_instance_id,
                    task_definition_name=task_definition_name
                )
            )
        except Exception as e:
            if self.metrics_collector is not None:
                self.metrics_collector.increment_task_execution_error(
                    task_definition_name, type(e)
                )
            task_result = TaskResult(
                task_id=task.task_id,
                workflow_instance_id=task.workflow_instance_id,
                worker_id=self.worker.get_identity()
            )
            task_result.status = 'FAILED'
            task_result.reason_for_incompletion = str(e)
            task_result.logs = [TaskExecLog(
                traceback.format_exc(), task_result.task_id, int(time.time()))]
            logger.error(
                'Failed to execute task, id: {task_id}, workflow_instance_id: {workflow_instance_id}, task_definition_name: {task_definition_name}, reason: {reason}'.format(
                    task_id=task.task_id,
                    workflow_instance_id=task.workflow_instance_id,
                    task_definition_name=task_definition_name,
                    reason=traceback.format_exc()
                )
            )
        return task_result

    def __update_task(self, task_result: TaskResult):
        if not isinstance(task_result, TaskResult):
            return None
        task_definition_name = self.worker.get_task_definition_name()
        logger.debug(
            'Updating task, id: {task_id}, workflow_instance_id: {workflow_instance_id}, task_definition_name: {task_definition_name}'.format(
                task_id=task_result.task_id,
                workflow_instance_id=task_result.workflow_instance_id,
                task_definition_name=task_definition_name
            )
        )
        for attempt in range(4):
            if attempt > 0:
                # Wait for [10s, 20s, 30s] before next attempt
                time.sleep(attempt * 10)
            try:
                response = self.task_client.update_task(body=task_result)
                logger.debug(
                    'Updated task, id: {task_id}, workflow_instance_id: {workflow_instance_id}, task_definition_name: {task_definition_name}, response: {response}'.format(
                        task_id=task_result.task_id,
                        workflow_instance_id=task_result.workflow_instance_id,
                        task_definition_name=task_definition_name,
                        response=response
                    )
                )
                return response
            except Exception as e:
                if self.metrics_collector is not None:
                    self.metrics_collector.increment_task_update_error(
                        task_definition_name, type(e)
                    )
                logger.error(
                    'Failed to update task, id: {task_id}, workflow_instance_id: {workflow_instance_id}, '
                    'task_definition_name: {task_definition_name}, reason: {reason}'.format(
                        task_id=task_result.task_id,
                        workflow_instance_id=task_result.workflow_instance_id,
                        task_definition_name=task_definition_name,
                        reason=traceback.format_exc()
                    )
                )
        return None

    def __wait_for_polling_interval(self) -> None:
        polling_interval = self.worker.get_polling_interval_in_seconds()
        time.sleep(polling_interval)

    def __set_worker_properties(self) -> None:
        # If multiple tasks are supplied to the same worker, then only first
        # task will be considered for setting worker properties
        task_type = self.worker.get_task_definition_name()

        domain = self.__get_property_value_from_env("domain", task_type)
        if domain:
            self.worker.domain = domain
        else:
            self.worker.domain = self.worker.get_domain()

        polling_interval = self.__get_property_value_from_env("polling_interval", task_type)
        if polling_interval:
            try:
                self.worker.poll_interval = float(polling_interval)
            except Exception as e:
                logger.error(f'error reading and parsing the polling interval value {polling_interval}')
                self.worker.poll_interval = self.worker.get_polling_interval_in_seconds()

        if polling_interval:
            try:
                self.worker.poll_interval = float(polling_interval)
                polling_interval_initialized = True
            except Exception as e:
                logger.error("Exception in reading polling interval from environment variable: {0}.".format(str(e)))

    def __get_property_value_from_env(self, prop, task_type):
        """
        get the property from the env variable
        e.g. conductor_worker_"prop" or conductor_worker_"task_type"_"prop"
        """
        prefix = "conductor_worker"
        # Look for generic property in both case environment variables
        key = prefix + "_" + prop
        value_all = os.getenv(key, os.getenv(key.upper()))

        # Look for task specific property in both case environment variables
        key_small = prefix + "_" + task_type + "_" + prop
        key_upper = prefix.upper() + "_" + task_type + "_" + prop.upper()
        value = os.getenv(key_small, os.getenv(key_upper, value_all))
        return value
