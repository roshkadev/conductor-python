from concurrent.futures import ThreadPoolExecutor, as_completed
from conductor.client.workflow.executor.workflow_executor import WorkflowExecutor
from conductor.client.configuration.configuration import Configuration
from conductor.client.http.api.workflow_resource_api import WorkflowResourceApi
from conductor.client.http.api_client import ApiClient
from conductor.client.workflow.conductor_workflow import ConductorWorkflow
import cv2
import base64
import time

def url_to_base64(url):
    img = cv2.imread(url)
    _, buffer = cv2.imencode('.jpg', img)
    bytes = buffer.tobytes()
    encoded = base64.b64encode(bytes).decode('utf-8')
    return encoded

# Read and encode the image as base64
front_url = "../digiyo/digiyo_datasets/digiyo_testing/4839850/front.jpg"
front_encoded = url_to_base64(front_url)

back_url = "../digiyo/digiyo_datasets/digiyo_testing/4839850/back.jpg"
back_encoded = url_to_base64(back_url)

selfie_url = "../digiyo/digiyo_datasets/digiyo_testing/4839850/selfie.jpg"
selfie_encoded = url_to_base64(selfie_url)

api_config = Configuration(server_api_url='https://conductor.digiyo.id/api')

# Define workflow input
# workflow_input = {
#     'front': front_encoded,
#     'back': back_encoded,
#     'selfie': selfie_encoded
# }

workflow_input = {
    'front': front_encoded,
    'back': back_encoded,
}

workflow_payload = {
    'name': 'id_check_workflow',
    'version': 8,
    'input': workflow_input
}

all_workflows = []



def run_forrest():
    api_client = ApiClient(configuration=api_config)
    workflow_resource_api = WorkflowResourceApi(api_client=api_client)
    workflow_id = workflow_resource_api.start_workflow(body=workflow_payload)
    all_workflows.append(workflow_id)

executor = ThreadPoolExecutor(max_workers=100)

workflow_count = 200
for i in range(workflow_count):
    executor.submit(run_forrest)

while len(all_workflows) < workflow_count:
    print(f"Still waitng for workflows to finish. {len(all_workflows)} out of {workflow_count} done.")
    time.sleep(5)
