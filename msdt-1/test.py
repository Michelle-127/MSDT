######################################################################################################################
#  Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.                                           #
#                                                                                                                    #
#  Licensed under the Apache License, Version 2.0 (the License). You may not use this file except in compliance    #
#  with the License. A copy of the License is located at                                                             #
#                                                                                                                    #
#      http://www.apache.org/licenses/LICENSE-2.0                                                                    #
#                                                                                                                    #
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES #
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions    #
#  and limitations under the License.                                                                                #
#####################################################################################################################

import copy
import datetime
import json
import logging
import os
import traceback
from typing import Any, Dict, List, Optional, Union

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Dict[str, Any], context: Any) -> None:
    """
    Handles incoming events and extracts Textract job information from the SNS message.
    """

    print("Received event: " + json.dumps(event))

    for record in event['Records']:
        message = json.loads(record['Sns']['Message'])
        print("Message: {}".format(message))

        request = {}

        request["job_id"] = message['job_id']
        request["jobTag"] = message['JobTag']
        request["jobStatus"] = message['Status']
        request["job_API"] = message['API']
        request["bucket_name"] = message['DocumentLocation']['S3Bucket']
        request["object_name"] = message['DocumentLocation']['S3object_name']
        request["output_bucket_name"] = os.environ['OUTPUT_BUCKET']

        print("Full path of input file is {}/{}".format(
            request["bucket_name"], request["object_name"]))

        process_request(request)


def get_job_results(api: str, job_id: str) -> List[Dict[str, Any]]:
    """
    Retrieves the Textract job results by job ID.
    """
    texttract_client = get_client('textract', 'us-east-1')
    blocks = []
    analysis = {}
    response = texttract_client.get_document_analysis(
        job_id=job_id
    )
    analysis = copy.deepcopy(response)
    while True:
        for block in response["Blocks"]:
            blocks.append(block)
        if ("NextToken" not in response.keys()):
            break
        next_token = response["NextToken"]
        response = texttract_client.get_document_analysis(
            job_id=job_id,
            NextToken=next_token
        )
    analysis.pop("NextToken", None)
    analysis["Blocks"] = blocks

    total_pages = response['DocumentMetadata']['Pages']
    final_JSON_allpage = []
    print(total_pages)
    for i in range(total_pages):
        this_page = i+1
        this_page_json = parse_json_in_order_per_page(analysis, this_page)
        final_JSON_allpage.append(
            {'Page': this_page, 'Content': this_page_json})
        print(f"Page {this_page} parsed")
    return final_JSON_allpage


def process_request(request: Dict[str, str]) -> Dict[str, Any]:
    """
    Processes a Textract job request and stores the results in S3
    and DynamoDB.
    """
    s3_client = get_client('s3', 'us-east-1')

    output = ""

    print("Request : {}".format(request))

    job_id = request['job_id']
    documentid = request['jobTag']
    jobStatus = request['jobStatus']
    job_API = request['job_API']
    bucket_name = request['bucket_name']
    output_bucket_name = request['output_bucket_name']
    object_name = request['object_name']

    directory = object_name.split('/')

    upload_path = ''
    for subdirectory in directory:
        if subdirectory != directory[-1]:
            upload_path += (subdirectory+'/')

    file_name = directory[-1]

    file_name_no_ext = file_name.rsplit(".", 1)[0]

    upload_path = upload_path + file_name_no_ext + '/textract/'

    final_JSON_allpage = get_job_results(job_API, job_id)

    analyses_bucket_name = output_bucket_name
    analyses_bucket_key = "{}".format(object_name.replace('.PDF', '.json'))
    s3_client.put_object(
        Bucket=analyses_bucket_name,
        Key=upload_path + analyses_bucket_key,
        Body=json.dumps(final_JSON_allpage).encode('utf-8')
    )

    write_to_dynamo_db("pdf-to-json", object_name,
                       bucket_name + '/' + object_name, final_JSON_allpage)

    return {
        'statusCode': 200,
        'body': json.dumps(final_JSON_allpage)
    }


def find_value_block(key_block: Dict, value_map: Dict) -> Dict:
    """
    Find the value block associated with a given key block.
    """
    for relationship in key_block['Relationships']:
        if relationship['Type'] == 'VALUE':
            for value_id in relationship['ids']:
                value_block = value_map[value_id]
    return value_block


def get_text(result: Dict, blocks_map: Dict[str, Dict]) -> str:
    """
    Retrieve text content from a block.
    """
    text = ''
    if 'Relationships' in result:
        for relationship in result['Relationships']:
            if relationship['Type'] == 'CHILD':
                for child_id in relationship['ids']:
                    word = blocks_map[child_id]
                    if word['BlockType'] == 'WORD':
                        text += word['Text'] + ' '
                    if word['BlockType'] == 'SELECTION_ELEMENT':
                        if word['SelectionStatus'] == 'SELECTED':
                            text += 'X '

    return text


def find_key_value_in_range(response: Dict, top: float, bottom: float, this_page: int) -> Dict[str, str]:
    """
    Find key-value pairs within a specified vertical range on a page.
    """
    blocks = response['Blocks']
    key_map = {}
    value_map = {}
    block_map = {}
    for block in blocks:
        if block['Page'] == this_page:
            block_id = block['id']
            block_map[block_id] = block
            if block['BlockType'] == "KEY_VALUE_SET" or block['BlockType'] == 'KEY' or block['BlockType'] == 'VALUE':
                if 'KEY' in block['EntityTypes']:
                    key_map[block_id] = block
                else:
                    value_map[block_id] = block

    kv_pair = {}
    for block_id, key_block in key_map.items():
        value_block = find_value_block(key_block, value_map)
        key = get_text(key_block, block_map)
        val = get_text(value_block, block_map)
        if (value_block['Geometry']['BoundingBox']['Top'] >= top and
                value_block['Geometry']['BoundingBox']['Top']+value_block['Geometry']['BoundingBox']['Height'] <= bottom):
            kv_pair[key] = val
    return kv_pair


def get_rows_columns_map(table_result: Dict, blocks_map: Dict[str, Dict]) -> Dict[int, Dict[int, str]]:
    """
    Map rows and columns of a table to their respective text content.
    """
    rows = {}
    for relationship in table_result['Relationships']:
        if relationship['Type'] == 'CHILD':
            for child_id in relationship['ids']:
                cell = blocks_map[child_id]
                if cell['BlockType'] == 'CELL':
                    row_index = cell['RowIndex']
                    col_index = cell['ColumnIndex']
                    if row_index not in rows:
                        rows[row_index] = {}
                    rows[row_index][col_index] = get_text(cell, blocks_map)
    return rows


def get_tables_from_json_in_range(response: Dict, top: float, bottom: float, this_page: int) -> Optional[List[List[str]]]:
    """
    Retrieve tables from a JSON response within a specified range.
    """
    blocks = response['Blocks']
    blocks_map = {}
    table_blocks = []
    for block in blocks:
        if block['Page'] == this_page:
            blocks_map[block['id']] = block
            if block['BlockType'] == "TABLE":
                if (block['Geometry']['BoundingBox']['Top'] >= top and
                    block['Geometry']['BoundingBox']['Top'] +
                        block['Geometry']['BoundingBox']['Height'] <= bottom):
                    table_blocks.append(block)

    if len(table_blocks) <= 0:
        return

    all_tables = []
    for table_result in table_blocks:
        table_matrix = []
        rows = get_rows_columns_map(table_result, blocks_map)
        for row_index, cols in rows.items():
            thisRow = []
            for col_index, text in cols.items():
                thisRow.append(text)
            table_matrix.append(thisRow)
        all_tables.append(table_matrix)
    return all_tables


def get_tables_coord_inrange(response: Dict, top: float, bottom: float, this_page: int) -> Optional[List[Dict]]:
    """
    Retrieve coordinates of tables within a specified range.
    """
    blocks = response['Blocks']
    blocks_map = {}
    table_blocks = []
    for block in blocks:
        if block['Page'] == this_page:
            blocks_map[block['id']] = block
            if block['BlockType'] == "TABLE":
                if (block['Geometry']['BoundingBox']['Top'] >= top and
                    block['Geometry']['BoundingBox']['Top'] +
                        block['Geometry']['BoundingBox']['Height'] <= bottom):
                    table_blocks.append(block)

    if len(table_blocks) <= 0:
        return

    all_tables_coord = []
    for table_result in table_blocks:

        all_tables_coord.append(table_result['Geometry']['BoundingBox'])
    return all_tables_coord


def box_with_in_box(box1: Dict, box2: Dict) -> bool:
    """
    Check if one bounding box is completely within another.
    """
    if box1['Top'] >= box2['Top'] and box1['Left'] >= box2['Left'] and box1['Top']+box1['Height'] <= box2['Top']+box2['Height'] and box1['Left']+box1['Width'] <= box2['Left']+box2['Width']:
        return True
    else:
        return False


def find_key_value_in_range_not_in_table(response: Dict, top: float, bottom: float, this_page: int) -> Dict[str, str]:
    """
    Find key-value pairs in a specified vertical range that are not part of a table.
    """
    blocks = response['Blocks']
    key_map = {}
    value_map = {}
    block_map = {}
    for block in blocks:
        if block['Page'] == this_page:
            block_id = block['id']
            block_map[block_id] = block
            if block['BlockType'] == "KEY_VALUE_SET" or block['BlockType'] == 'KEY' or block['BlockType'] == 'VALUE':
                if 'KEY' in block['EntityTypes']:
                    key_map[block_id] = block
                else:
                    value_map[block_id] = block

    all_tables_coord = get_tables_coord_inrange(
        response, top, bottom, this_page)

    kv_pair = {}
    for block_id, key_block in key_map.items():
        value_block = find_value_block(key_block, value_map)
        key = get_text(key_block, block_map)
        val = get_text(value_block, block_map)
        if (value_block['Geometry']['BoundingBox']['Top'] >= top and
                value_block['Geometry']['BoundingBox']['Top']+value_block['Geometry']['BoundingBox']['Height'] <= bottom):

            kv_overlap_table_list = []
            if all_tables_coord is not None:
                for table_coord in all_tables_coord:
                    kv_overlap_table_list.append(box_with_in_box(
                        value_block['Geometry']['BoundingBox'], table_coord))
                if sum(kv_overlap_table_list) == 0:
                    kv_pair[key] = val
            else:
                kv_pair[key] = val
    return kv_pair


def parse_json_in_order_per_page(response: Dict[str, Any], this_page: int) -> List[Dict[str, Any]]:
    """
    Parses the Textract response for a specific page and returns the text in order.
    """
    text_list = []
    id_list_kv_table = []
    for block in response['Blocks']:
        if block['Page'] == this_page:
            if block['BlockType'] == 'TABLE' or block['BlockType'] == 'CELL' or \
               block['BlockType'] == 'KEY_VALUE_SET' or block['BlockType'] == 'KEY' or block['BlockType'] == 'VALUE' or  \
               block['BlockType'] == 'SELECTION_ELEMENT':

                kv_id = block['id']
                if kv_id not in id_list_kv_table:
                    id_list_kv_table.append(kv_id)

                child_idlist = []
                if 'Relationships' in block.keys():
                    for child in block['Relationships']:
                        child_idlist.append(child['ids'])
                    flat_child_idlist = [
                        item for sublist in child_idlist for item in sublist]
                    for childid in flat_child_idlist:
                        if childid not in id_list_kv_table:
                            id_list_kv_table.append(childid)
    text_list = []
    for block in response['Blocks']:
        if block['Page'] == this_page:
            if block['BlockType'] == 'LINE':

                thisline_idlist = []
                thisline_idlist.append(block['id'])
                child_idlist = []
                if 'Relationships' in block.keys():
                    for child in block['Relationships']:
                        child_idlist.append(child['ids'])
                    flat_child_idlist = [
                        item for sublist in child_idlist for item in sublist]
                    for childid in flat_child_idlist:
                        thisline_idlist.append(childid)

                set_line_id = set(thisline_idlist)
                set_all_kv_table_id = set(id_list_kv_table)
                if len(set_line_id.intersection(set_all_kv_table_id)) == 0:
                    thisDict = {'Line': block['Text'],
                                'Left': block['Geometry']['BoundingBox']['Left'],
                                'Top': block['Geometry']['BoundingBox']['Top'],
                                'Width': block['Geometry']['BoundingBox']['Width'],
                                'Height': block['Geometry']['BoundingBox']['Height']}
                    text_list.append(thisDict)

    final_JSON = []
    for i in range(len(text_list)-1):
        this_text = text_list[i]['Line']
        this_top = text_list[i]['Top']
        this_bottom = text_list[i+1]['Top']+text_list[i+1]['Height']
        this_text_KV = find_key_value_in_range(
            response, this_top, this_bottom, this_page)
        this_text_table = get_tables_from_json_in_range(
            response, this_top, this_bottom, this_page)
        final_JSON.append(
            {this_text: {'KeyValue': this_text_KV, 'Tables': this_text_table}})

    if (len(text_list) > 0):
        last_text = text_list[len(text_list)-1]['Line']
        last_top = text_list[len(text_list)-1]['Top']
        last_bottom = 1
        this_text_KV = find_key_value_in_range(
            response, last_top, last_bottom, this_page)
        this_text_table = get_tables_from_json_in_range(
            response, last_top, last_bottom, this_page)
        final_JSON.append(
            {last_text: {'KeyValue': this_text_KV, 'Tables': this_text_table}})

    return final_JSON


def write_to_dynamo_db(
    dd_table_name: str, id: str, full_file_path: str, full_pdf_json: Any
) -> None:
    """
    Writes data to a DynamoDB table, creating the table if it does not exist.
    """
    dynamodb = get_resource('dynamodb')
    dynamodb_client = get_client('dynamodb')

    dd_table_name = dd_table_name \
        .replace(" ", "-") \
        .replace("(", "-") \
        .replace(")", "-") \
        .replace("&", "-") \
        .replace(",", " ") \
        .replace(":", "-") \
        .replace('/', '--') \
        .replace("#", 'No') \
        .replace('"', 'Inch')

    if len(dd_table_name) <= 3:
        dd_table_name = dd_table_name + '-xxxx'

    print("DynamoDB table name is {}".format(dd_table_name))
    try:

        existing_tables = list([x.name for x in dynamodb.tables.all()])

        if dd_table_name not in existing_tables:
            table = dynamodb.create_table(
                table_name=dd_table_name,
                key_schema=[
                    {
                        'AttributeName': 'id',
                        'KeyType': 'HASH'
                    }
                ],
                attribute_definitions=[
                    {
                        'AttributeName': 'id',
                        'AttributeType': 'S'
                    },
                ],
                billing_mode='PAY_PER_REQUEST',
            )
            table.meta.client.get_waiter(
                'table_exists').wait(table_name=dd_table_name)
            print("Table successfully created. Item count is: " +
                  str(table.item_count))
    except ClientError as e:
        if e.response['Error']['Code'] in ["ThrottlingException", "ProvisionedThroughputExceededException"]:
            msg = f"DynamoDB ] Write Failed from DynamoDB, Throttling Exception [{
                e}] [{traceback.format_exc()}]"
            logging.warning(msg)
            raise e
        else:
            msg = f"DynamoDB Write Failed from DynamoDB Exception [{
                e}] [{traceback.format_exc()}]"
            logging.error(msg)
            raise e

    except Exception as e:
        msg = f"DynamoDB Write Failed from DynamoDB Exception [{
            e}] [{traceback.format_exc()}]"
        logging.error(msg)
        raise Exception(e)

    table = dynamodb.Table(dd_table_name)

    try:
        table.put_item(Item={
            'id': id,
            'FilePath': full_file_path,
            'PdfJsonRegularFormat': str(full_pdf_json),
            'PdfJsonDynamoFormat': full_pdf_json,
            'DateTime': datetime.datetime.utcnow().isoformat(),
        }
        )
    except ClientError as e:
        if e.response['Error']['Code'] in ["ThrottlingException", "ProvisionedThroughputExceededException"]:
            msg = f"DynamoDB ] Write Failed from DynamoDB, Throttling Exception [{
                e}] [{traceback.format_exc()}]"
            logging.warning(msg)
            raise e

        else:
            msg = f"DynamoDB Write Failed from DynamoDB Exception [{
                e}] [{traceback.format_exc()}]"
            logging.error(msg)
            raise e

    except Exception as e:
        msg = f"DynamoDB Write Failed from DynamoDB Exception [{
            e}] [{traceback.format_exc()}]"
        logging.error(msg)
        raise Exception(e)


def dict_to_item(raw: Union[Dict, str, int]) -> Union[Dict, List[Dict]]:
    """
    Convert a Python dictionary or primitive types into a DynamoDB-compatible format.
    """
    if type(raw) is dict:
        resp = {}
        for k, v in raw.items():
            if type(v) is str:
                resp[k] = {
                    'S': v
                }
            elif type(v) is int:
                resp[k] = {
                    'I': str(v)
                }
            elif type(v) is dict:
                resp[k] = {
                    'M': dict_to_item(v)
                }
            elif type(v) is list:
                resp[k] = []
                for i in v:
                    resp[k].append(dict_to_item(i))

        return resp
    elif type(raw) is str:
        return {
            'S': raw
        }
    elif type(raw) is int:
        return {
            'I': str(raw)
        }


def get_client(name: str, aws_region: Optional[str] = None) -> boto3.client:
    """
    Creates a Boto3 client for a specified AWS service.
    """
    config = Config(
        retries=dict(
            max_attempts=30
        )
    )
    if (aws_region):
        return boto3.client(name, region_name=aws_region, config=config)
    else:
        return boto3.client(name, config=config)


def get_resource(name: str, aws_region: Optional[str] = None) -> boto3.resources.base.ServiceResource:
    """
    Get a Boto3 resource with optional region configuration.
    """
    config = Config(
        retries=dict(
            max_attempts=30
        )
    )

    if (aws_region):
        return boto3.resource(name, region_name=aws_region, config=config)
    else:
        return boto3.resource(name, config=config)
