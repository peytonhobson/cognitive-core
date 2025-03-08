import os
import boto3
import json
import asyncio
import tempfile
import pandas as pd
from dotenv import load_dotenv
from utils import chunk_documents, generate_document_embeddings, upsert_embeddings
from langchain.schema import Document
from langchain_community.document_loaders import PyMuPDFLoader

load_dotenv()

# Initialize AWS clients
sqs = boto3.client('sqs', region_name='us-east-1')
s3 = boto3.client('s3')

QUEUE_URL = os.getenv('QUEUE_URL')


async def process_sqs_messages():
    """Poll and process messages from SQS queue."""
    print("Starting SQS message polling...")
    try:
        has_more_messages = True
        message_count = 0

        while has_more_messages:
            print(f"Polling for messages from queue: {QUEUE_URL}")
            # Receive messages from SQS
            response = sqs.receive_message(
                QueueUrl=QUEUE_URL,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=10
            )

            messages = response.get('Messages', [])
            
            if not messages:
                print("No messages received, ending polling")
                has_more_messages = False
                continue

            print(f"Received {len(messages)} messages from SQS")
            message_count += len(messages)
            
            # Process all messages in batch
            successfully_processed = await process_messages(messages)

            # Delete successfully processed messages
            success_count = 0
            for message, success in zip(messages, successfully_processed):
                receipt_handle = message.get('ReceiptHandle')
                if receipt_handle and success:
                    sqs.delete_message(
                        QueueUrl=QUEUE_URL,
                        ReceiptHandle=receipt_handle
                    )
                    success_count += 1
                elif not success:
                    print(f"Failed to process message: {message.get('MessageId')}")
            
            print(f"Successfully processed and deleted {success_count} of {len(messages)} messages")
        
        print(f"Finished processing a total of {message_count} messages")
    except Exception as error:
        print(f'Error polling messages: {error}')


def parse_message(message):
    """Parse a single SQS message and extract file information."""
    message_id = message.get('MessageId', 'unknown')
    print(f"Parsing message {message_id}")
    try:
        message_body = message.get('Body')
        if not message_body:
            print(f"Message {message_id} has no body")
            return None
            
        body = json.loads(message_body)
        file_info = {
            'bucket_name': body['bucket'],
            'object_key': body['key'],
            'message_id': message_id
        }
        print(f"Successfully parsed message {message_id}: {file_info['bucket_name']}/{file_info['object_key']}")
        return file_info
    except Exception as e:
        print(f"Error parsing message {message_id}: {e}")
        return None


async def process_text_file(bucket_name, object_key, file_content, file_extension):
    """Process a text-based file."""
    print(f"Processing text file: {object_key} ({file_extension})")
    return Document(
        page_content=file_content,
        metadata={
            "bucketName": bucket_name,
            "objectKey": object_key,
            "source": f"s3://{bucket_name}/{object_key}",
            "file_type": file_extension[1:]  # Remove the dot
        }
    )


async def process_pdf_file(bucket_name, object_key, file_content):
    """Process a PDF file."""
    print(f"Processing PDF file: {object_key}")
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_file:
        temp_file.write(file_content)
        temp_path = temp_file.name
        print(f"Saved PDF to temporary file: {temp_path}")
    
    try:
        loader = PyMuPDFLoader(temp_path)
        pdf_docs = loader.load()
        print(f"Loaded {len(pdf_docs)} pages from PDF: {object_key}")
        
        # Add metadata to each page
        for pdf_doc in pdf_docs:
            pdf_doc.metadata.update({
                "bucketName": bucket_name,
                "objectKey": object_key,
                "source": f"s3://{bucket_name}/{object_key}",
                "file_type": "pdf"
            })
        
        return pdf_docs
    finally:
        # Clean up the temporary file
        os.unlink(temp_path)
        print(f"Removed temporary file: {temp_path}")


async def process_excel_file(bucket_name, object_key, file_content, file_extension):
    """Process an Excel file."""
    print(f"Processing Excel file: {object_key} ({file_extension})")
    docs = []
    with tempfile.NamedTemporaryFile(suffix=file_extension, delete=False) as temp_file:
        temp_file.write(file_content)
        temp_path = temp_file.name
        print(f"Saved Excel file to temporary file: {temp_path}")
    
    try:
        sheets = pd.read_excel(temp_path, sheet_name=None)
        print(f"Found {len(sheets)} sheets in Excel file: {object_key}")
        
        for sheet_name, df in sheets.items():
            print(f"Processing sheet: {sheet_name} with {len(df)} rows")
            text = f"Sheet: {sheet_name}\n" + df.to_csv(index=False)
            doc = Document(
                page_content=text,
                metadata={
                    "bucketName": bucket_name,
                    "objectKey": object_key,
                    "source": f"s3://{bucket_name}/{object_key}",
                    "file_type": file_extension[1:],
                    "sheet_name": sheet_name
                }
            )
            docs.append(doc)
        return docs
    finally:
        # Clean up the temporary file
        os.unlink(temp_path)
        print(f"Removed temporary file: {temp_path}")


async def download_and_process_file(file_info):
    """Download a file from S3 and process it based on its type."""
    bucket_name = file_info['bucket_name']
    object_key = file_info['object_key']
    
    print(f"Downloading file from bucket '{bucket_name}' with key '{object_key}'")
    
    try:
        # Download file from S3
        response = s3.get_object(Bucket=bucket_name, Key=object_key)
        file_content = response['Body'].read()
        content_length = len(file_content)
        print(f"Downloaded {content_length} bytes for file '{object_key}'")
        
        # Determine file type from object key
        file_extension = os.path.splitext(object_key)[1].lower()
        print(f"Detected file extension: {file_extension}")
        
        # Process file based on its type
        if file_extension in ['.txt', '.md', '.json', '.csv']:
            # Text files can be decoded
            print(f"Processing as text file: {object_key}")
            text_content = file_content.decode('utf-8')
            doc = await process_text_file(bucket_name, object_key, text_content, file_extension)
            return [doc], True
        
        elif file_extension == '.pdf':
            print(f"Processing as PDF file: {object_key}")
            pdf_docs = await process_pdf_file(bucket_name, object_key, file_content)
            return pdf_docs, True
        
        elif file_extension in ['.xlsx', '.xls']:
            print(f"Processing as Excel file: {object_key}")
            excel_docs = await process_excel_file(bucket_name, object_key, file_content, file_extension)
            return excel_docs, True
        
        else:
            # Unsupported file type
            print(f"Unsupported file type: {file_extension} for {object_key}")
            return [], False
            
    except Exception as e:
        print(f"Error processing file {object_key}: {e}")
        return [], False


async def generate_and_upsert_embeddings(chunked_docs, file_info):
    """Generate embeddings for document chunks and upsert them to the database."""
    bucket_name = file_info['bucket_name']
    object_key = file_info['object_key']
    
    print(f"Generating embeddings for {len(chunked_docs)} chunks from file '{object_key}'")
    
    # Extract content from chunks
    chunk_contents = [doc.page_content for doc in chunked_docs]
    
    # Generate embeddings
    print(f"Calling OpenAI API to generate embeddings for {len(chunk_contents)} chunks")
    embeddings = await generate_document_embeddings(chunk_contents)
    
    if not embeddings:
        print(f"Failed to generate embeddings for file '{object_key}'")
        return False
    
    print(f"Successfully generated {len(embeddings)} embeddings, upserting to Pinecone")
    
    # Upsert embeddings for each chunk
    try:
        results = []
        for idx, vector in enumerate(embeddings):
            print(f"Upserting chunk {idx} for {object_key}")
            result = upsert_embeddings(
                id=f"{object_key}-chunk-{idx}",
                vector=vector,
                metadata={
                    "bucketName": bucket_name, 
                    "objectKey": object_key, 
                    "chunkIndex": idx,
                    "text": chunked_docs[idx].page_content[:100]  # Store preview of text
                }
            )
            print(f"Upsert result for chunk {idx}: {result}")
            results.append(result)
        
        success_count = sum(1 for result in results if result)
        print(f"Successfully upserted {success_count}/{len(chunked_docs)} embeddings for file '{object_key}'")
        
        # Consider the operation successful if at least one chunk was upserted
        return success_count > 0
    except Exception as e:
        print(f"Error upserting embeddings: {e}")
        import traceback
        traceback.print_exc()
        return False


async def process_messages(messages: list) -> list:
    """Process multiple messages from SQS."""
    print(f"Processing {len(messages)} messages")
    results = []
    
    # Parse all messages
    file_infos = []
    for message in messages:
        file_info = parse_message(message)
        if file_info:
            file_infos.append(file_info)
            results.append(True)  # Placeholder, will update based on processing result
        else:
            results.append(False)
    
    print(f"Successfully parsed {len(file_infos)} of {len(messages)} messages")
    
    # Process each file
    all_docs = []
    successful_file_infos = []
    
    for i, file_info in enumerate(file_infos):
        print(f"Processing file {i+1}/{len(file_infos)}: {file_info['object_key']}")
        docs, success = await download_and_process_file(file_info)
        
        if success and docs:
            print(f"Successfully processed file {file_info['object_key']}, got {len(docs)} documents")
            all_docs.extend(docs)
            successful_file_infos.append(file_info)
        else:
            print(f"Failed to process file {file_info['object_key']}")
            results[i] = False
    
    if not all_docs:
        print("No documents were successfully processed")
        return results
    
    print(f"Processing a total of {len(all_docs)} documents from {len(successful_file_infos)} files")
    
    try:
        # Process all documents together
        print("Chunking documents...")
        chunked_docs = chunk_documents(all_docs)
        print(f"Created {len(chunked_docs)} chunks from {len(all_docs)} documents")
        
        # Group chunks by document
        print("Grouping chunks by document...")
        doc_chunks = {}
        for chunk in chunked_docs:
            object_key = chunk.metadata.get("objectKey")
            if object_key not in doc_chunks:
                doc_chunks[object_key] = []
            doc_chunks[object_key].append(chunk)
        
        print(f"Grouped chunks into {len(doc_chunks)} documents")
        
        # Process each document's chunks
        for i, file_info in enumerate(file_infos):
            if not results[i]:
                continue  # Skip failed files
                
            object_key = file_info['object_key']
            if object_key in doc_chunks:
                print(f"Processing chunks for document: {object_key}")
                success = await generate_and_upsert_embeddings(doc_chunks[object_key], file_info)
                results[i] = success
            else:
                print(f"No chunks found for document: {object_key}")
            
    except Exception as e:
        print(f"Error processing documents: {e}")
        import traceback
        traceback.print_exc()  # Print the full traceback for better debugging
        # Mark all remaining documents as failed
        for i in range(len(results)):
            if results[i]:  # Only update those that weren't already marked as failed
                results[i] = False
    
    success_count = sum(1 for result in results if result)
    print(f"Finished processing messages: {success_count}/{len(messages)} successful")
    return results

if __name__ == "__main__":
    asyncio.run(process_sqs_messages())