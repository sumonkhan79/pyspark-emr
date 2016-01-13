import argparse
import boto3
import subprocess
import getpass
import time

def _get_client(aws_region):
  return boto3.client('emr', region_name=aws_region)

def add_step_to_job_flow(job_flow_id=None,
                         python_path=None,
                         spark_main=None,
                         spark_main_args=None,
                         s3_work_bucket=None,
                         aws_region=None):
  assert(job_flow_id)
  assert(aws_region)

  job_flow_name = _create_job_flow_name(spark_main)
  steps = _create_steps(job_flow_name=job_flow_name,
                        python_path=python_path,
                        spark_main=spark_main,
                        spark_main_args=spark_main_args,
                        s3_work_bucket=s3_work_bucket)
  client = _get_client(aws_region)
  step_response = client.add_job_flow_steps(
    JobFlowId=job_flow_id,
    Steps=steps
  )
  step_ids = step_response['StepIds']
  print "Created steps: {}".format(step_ids)
  _wait_for_job_flow(aws_region, job_flow_id, step_ids)

def _create_job_flow_name(spark_main):
  return '{}.{}.{}'.format(getpass.getuser(), spark_main, int(time.time()))

def _create_steps(job_flow_name=None,
                  python_path=None,
                  spark_main=None,
                  spark_main_args=None,
                  s3_work_bucket=None):
  assert(python_path)
  assert(spark_main)
  assert(s3_work_bucket)

  zip_file = 'spark_zip.zip'
  # TODO: Change these subprocess calls to use python native API instead of shell
  subprocess.call('rm /tmp/{}'.format(zip_file), shell=True)
  subprocess.check_call("cd {}; zip -r /tmp/{} . -i '*.py'".format(python_path, zip_file), shell=True)
  sources_rel_path = job_flow_name
  s3sources = 's3://{}/sources/{}'.format(s3_work_bucket, sources_rel_path)
  zip_file_on_s3 = '{}/{}'.format(s3sources, zip_file)
  print 'Storing python sources on {}'.format(s3sources)
  subprocess.check_call('aws s3 cp /tmp/{} {}'.format(zip_file, zip_file_on_s3), shell=True)
  sources_on_host = '/home/hadoop/{}'.format(sources_rel_path)
  zip_file_on_host = '{}/{}'.format(sources_on_host, zip_file)
  spark_main_on_host = '{}/{}'.format(sources_on_host, spark_main)
  spark_main_args = spark_main_args.split() if spark_main_args else ['']
  return [
    {
      'Name': 'setup - copy files',
      'ActionOnFailure': 'CANCEL_AND_WAIT',
      'HadoopJarStep': {
        'Jar': 'command-runner.jar',
        'Args': ['aws', 's3', 'cp', zip_file_on_s3, sources_on_host + '/']
      }
    },
    {
      'Name': 'setup - extract files',
      'ActionOnFailure': 'CANCEL_AND_WAIT',
      'HadoopJarStep': {
        'Jar': 'command-runner.jar',
        'Args': ['unzip', zip_file_on_host, '-d', sources_on_host]
      }
    },
    {
      'Name': 'run spark '.format(spark_main),
      'ActionOnFailure': 'CANCEL_AND_WAIT',
      'HadoopJarStep': {
        'Jar': 'command-runner.jar',
        'Args': ['spark-submit', '--py-files', zip_file_on_host,
            spark_main_on_host] + spark_main_args
      }
    },
  ]

def create_cluster_and_run_job_flow(create_cluster_hosts_type=None,
                                    create_cluster_num_hosts=1,
                                    create_cluster_ec2_key_name=None,
                                    create_cluster_ec2_subnet_id=None,
                                    python_path=None,
                                    spark_main=None,
                                    spark_main_args=None,
                                    s3_work_bucket=None,
                                    aws_region=None):
  assert(create_cluster_hosts_type)
  assert(aws_region)

  s3_logs_uri = 's3n://{}/logs/{}/'.format(s3_work_bucket, getpass.getuser())
  job_flow_name = _create_job_flow_name(spark_main)
  steps = _create_steps(job_flow_name=job_flow_name,
                        python_path=python_path,
                        spark_main=spark_main,
                        spark_main_args=spark_main_args,
                        s3_work_bucket=s3_work_bucket)
  client = _get_client(aws_region)
  response = client.run_job_flow(
      Name=job_flow_name,
      LogUri=s3_logs_uri,
      ReleaseLabel='emr-4.2.0',
      Instances={
        'MasterInstanceType': create_cluster_hosts_type,
        'SlaveInstanceType': create_cluster_hosts_type,
        'InstanceCount': create_cluster_num_hosts,
        'Ec2KeyName': create_cluster_ec2_key_name,
        'KeepJobFlowAliveWhenNoSteps': False,
        'TerminationProtected': False,
        'Ec2SubnetId': create_cluster_ec2_subnet_id,
      },
      Steps=[
        {
          'Name': 'Setup Debugging',
          'ActionOnFailure': 'TERMINATE_CLUSTER',
          'HadoopJarStep': {
              'Jar': 'command-runner.jar',
              'Args': ['state-pusher-script']
          }
        },
      ] + steps,
      Applications=[{'Name': 'Ganglia'}, {'Name': 'Spark'}],
      Configurations=[
        {
          'Classification': 'spark',
          'Properties': {
              'maximizeResourceAllocation': 'true'
          }
        },
      ],
      VisibleToAllUsers=True,
      JobFlowRole='EMR_EC2_DefaultRole',
      ServiceRole='EMR_DefaultRole',
      Tags=[{'Key': 'Name', 'Value': spark_main},
    ]
  )
  job_flow_id = response['JobFlowId']
  print 'Created Job Flow: {}'.format(job_flow_id)
  step_ids = _get_step_ids_for_job_flow(job_flow_id, client)
  print 'Created Job steps: {}'.format(step_ids)
  print "Waiting for steps to finish. Visit on aws portal: https://{0}.console.aws.amazon.com/elasticmapreduce/home?region={0}#cluster-details:{1}".format(aws_region, job_flow_id)
  _wait_for_job_flow(aws_region, job_flow_id, step_ids)


def _get_step_ids_for_job_flow(job_flow_id, client):
  steps = client.list_steps(ClusterId=job_flow_id)
  step_ids = map(lambda s: s['Id'], steps['Steps'])
  return step_ids


def _wait_for_job_flow(aws_region, job_flow_id, step_ids=[]):
  while True:
    time.sleep(5)
    client = _get_client(aws_region)
    cluster = client.describe_cluster(ClusterId=job_flow_id)
    state = cluster['Cluster']['Status']['State']
    p = []
    p.append('Cluster: {}'.format(state))
    all_done = True
    for step_id in step_ids:
      step = client.describe_step(ClusterId=job_flow_id, StepId=step_id)
      step_state = step['Step']['Status']['State']
      step_done = step_state in ['COMPLETED', 'FAILED']
      step_failed = step_state == 'FAILED'
      p.append('{} ({}) - {}'.format(step['Step']['Name'],
                                     step['Step']['Id'],
                                     step_state))
      all_done = all_done and step_done
      if step_failed:
        print '!!! STEP FAILED !!!'
    print '\t'.join(p)
    if all_done:
      print "All done"
      break


if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--create_cluster',
                      help='Create a new cluster (and destroy it when it is done',
                      action='store_true')
  parser.add_argument('--create_cluster_hosts_type', help='Number of hosts',
                      default='m3.xlarge')
  parser.add_argument('--create_cluster_num_hosts', help='Number of hosts',
                      type=int, default=1)
  parser.add_argument('--create_cluster_ec2_key_name', help='Keyfile when you want to create a new cluster and connect to it')
  parser.add_argument('--create_cluster_ec2_subnet_id', help='')
  parser.add_argument('--aws_region', help='AWS region', required=True)

  parser.add_argument('--job_flow_id',
                      help='Job flow ID (EMR cluster) to submit to')
  parser.add_argument('--python_path', required=True,
                      help='Path to python files to zip and upload to the server and add to the python path. This should include the python_main file`')
  parser.add_argument('--spark_main', required=True,
                      help='Main python file for spark')
  parser.add_argument('--spark_main_args',
                      help='Arguments passed to your spark script')
  parser.add_argument('--s3_work_bucket', required=True,
                      help='Name of s3 bucket where sources and logs are uploaded')
  args = parser.parse_args()

  if args.job_flow_id:
    add_step_to_job_flow(job_flow_id=args.job_flow_id,
                         python_path=args.python_path,
                         spark_main=args.spark_main,
                         spark_main_args=args.spark_main_args,
                         s3_work_bucket=args.s3_work_bucket,
                         aws_region=args.aws_region)
  elif args.create_cluster:
    create_cluster_and_run_job_flow(
        create_cluster_hosts_type=args.create_cluster_hosts_type,
        create_cluster_num_hosts=args.create_cluster_num_hosts,
        create_cluster_ec2_key_name=args.create_cluster_ec2_key_name,
        create_cluster_ec2_subnet_id=args.create_cluster_ec2_subnet_id,
        python_path=args.python_path,
        spark_main=args.spark_main,
        spark_main_args=args.spark_main_args,
        s3_work_bucket=args.s3_work_bucket,
        aws_region=args.aws_region)
  else:
    print "Nothing to do"
    parser.print_help()
