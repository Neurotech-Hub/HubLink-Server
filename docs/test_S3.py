import boto3
import csv
import os

try:
    # Prompt for the CSV file path
    csv_path = input("Enter the path to your AWS credentials CSV file: ").strip('"').strip("'")

    # Ensure the file exists
    if not os.path.exists(csv_path):
        raise FileNotFoundError("The file path you entered does not exist.")

    # Read credentials from the CSV
    try:
        with open(csv_path, 'r') as file:
            reader = csv.reader(file)
            next(reader)  # Skip the header row
            try:
                credentials = next(reader)  # Read the second row
                ACCESS_KEY = credentials[0]
                SECRET_KEY = credentials[1]
            except (StopIteration, IndexError):
                raise ValueError("CSV file is empty or does not contain credentials in the expected format")
    except csv.Error:
        raise ValueError("Error reading CSV file. Please ensure it's properly formatted")

    # Prompt for the bucket name
    BUCKET_NAME = input("Enter the S3 bucket name: ").strip()
    if not BUCKET_NAME:
        raise ValueError("Bucket name cannot be empty")

    # Create a test file
    file_name = "test_file.csv"
    try:
        with open(file_name, "w") as f:
            f.write("column1,column2,column3\nvalue1,value2,value3")
    except IOError as e:
        raise IOError(f"Error creating test file: {str(e)}")

    # Initialize S3 client
    try:
        s3 = boto3.client(
            's3',
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY
        )

        # Upload the file
        s3.upload_file(file_name, BUCKET_NAME, file_name)
        print(f"Successfully uploaded {file_name} to {BUCKET_NAME}.")

    except boto3.exceptions.S3UploadFailedError as e:
        raise Exception(f"Failed to upload file to S3: {str(e)}")
    except boto3.exceptions.BotoCoreError as e:
        raise Exception(f"AWS configuration error: {str(e)}")
    except boto3.exceptions.ClientError as e:
        raise Exception(f"AWS client error: {str(e)}")

except Exception as e:
    print(f"Error: {str(e)}")
    exit(1)
finally:
    # Clean up the test file
    try:
        if os.path.exists(file_name):
            os.remove(file_name)
    except OSError:
        print(f"Warning: Could not remove temporary file {file_name}")
