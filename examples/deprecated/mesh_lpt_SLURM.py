import numpy as np
import os
import math
import tensorflow.compat.v1 as tf
import mesh_tensorflow as mtf
import flowpm.mesh_ops as mpm
import flowpm
from astropy.cosmology import Planck15
from flowpm.tfpm import PerturbationGrowth
from flowpm import linear_field, lpt_init, nbody, cic_paint
from scipy.interpolate import InterpolatedUnivariateSpline as iuspline
from matplotlib import pyplot as plt
cosmology=Planck15

tf.flags.DEFINE_integer("gpus_per_node", 8, "Number of GPU on each node")
tf.flags.DEFINE_integer("gpus_per_task", 8, "Number of GPU in each task")
tf.flags.DEFINE_integer("tasks_per_node", 1, "Number of task in each node")

tf.flags.DEFINE_integer("nc", 64, "Size of the cube")
tf.flags.DEFINE_integer("batch_size", 8, "Batch Size")

FLAGS = tf.flags.FLAGS

def lpt_prototype(nc=64, batch_size=8, a=1.0, nproc=2):
  """
  Prototype of function computing LPT deplacement.

  Returns output tensorflow and mesh tensorflow tensors
  """
  # Compute a few things first, using simple tensorflow
  klin = np.loadtxt('../flowpm/data/Planck15_a1p00.txt').T[0]
  plin = np.loadtxt('../flowpm/data/Planck15_a1p00.txt').T[1]
  ipklin = iuspline(klin, plin)
  pt = PerturbationGrowth(cosmology, a=[a], a_normalize=1.0)
  # Generate a batch of 3D initial conditions
  initial_conditions = flowpm.linear_field(nc,          # size of the cube
                                           200,         # Physical size of the cube
                                           ipklin,      # Initial power spectrum
                                           batch_size=batch_size)
  # Sample particles uniformly on the grid
  particle_positions = tf.cast(tf.stack(tf.meshgrid(tf.range(nc), tf.range(nc), tf.range(nc)), axis=-1), dtype=tf.float32)
  particle_positions = tf.tile(tf.expand_dims(particle_positions, axis=0), [batch_size, 1, 1, 1, 1])
  # Compute lpt displacement term and apply cosmological scaling
  dx = flowpm.tfpm.lpt1(flowpm.utils.r2c3d(initial_conditions, norm=nc**3), particle_positions)
  dx = pt.D1(a)*dx
  # Move the particles according to the displacement
  particle_positions = tf.reshape(particle_positions, (batch_size, -1, 3)) + dx
  # Paint the particles back onto a mesh
  final_field = cic_paint(tf.zeros_like(initial_conditions), particle_positions)
  # Compute necessary Fourier kernels
  kvec = flowpm.kernels.fftk((nc, nc, nc), symmetric=False)
  from flowpm.kernels import laplace_kernel, gradient_kernel
  lap = tf.cast(laplace_kernel(kvec), tf.complex64)
  grad_x = gradient_kernel(kvec, 0)
  grad_y = gradient_kernel(kvec, 1)
  grad_z = gradient_kernel(kvec, 2)

  ### Ok, now we implement the same thing but using Mesh TensorFlow ###
  graph = mtf.Graph()
  mesh = mtf.Mesh(graph, "my_mesh")

  # Define the named dimensions
  batch_dim = mtf.Dimension("batch", batch_size)
  x_dim = mtf.Dimension("nx", nc)
  y_dim = mtf.Dimension("ny", nc)
  z_dim = mtf.Dimension("nz", nc)

  # Import initial conditions and Fourier kernels from simple tensorflow tensors
  rfield = mtf.import_tf_tensor(mesh, initial_conditions , shape=[batch_dim, x_dim, y_dim, z_dim])
  mlx = mtf.import_tf_tensor(mesh, grad_x*lap, shape=[x_dim, y_dim, z_dim])
  mly = mtf.import_tf_tensor(mesh, grad_y*lap, shape=[x_dim, y_dim, z_dim])
  mlz = mtf.import_tf_tensor(mesh, grad_z*lap, shape=[x_dim, y_dim, z_dim])

  # Create a list of particles for each slice of the data
  mstate = mpm.mtf_indices(mesh, shape=[x_dim, y_dim, z_dim], dtype=tf.float32)
  mstate = mtf.einsum([mtf.ones(mesh, [batch_dim]), mstate], output_shape=[batch_dim] + mstate.shape[:])

  # Compute displacement by applying a series of fourier kernels, and taking the inverse fourier transform
  lineark = mpm.fft3d(rfield)
  displacement = [mtf.cast(mpm.ifft3d(mtf.multiply(lineark,mlx)), tf.float32),
                  mtf.cast(mpm.ifft3d(mtf.multiply(lineark,mly)), tf.float32),
                  mtf.cast(mpm.ifft3d(mtf.multiply(lineark,mlz)), tf.float32)]
  displacement = mtf.stack(displacement, dim_name="ndim", axis=4)

  # Apply displacement to input particles, scaled by cosmology
  mfstate = mstate + pt.D1(a)*displacement

  # Paint the particles onto a new field, taking care of border effects
  nproc = int(math.sqrt(nproc))
  mesh_final_field = mpm.cic_paint(mtf.zeros_like(rfield), mfstate, [x_dim], [nproc])

  return initial_conditions, final_field, mesh_final_field


def main(_):
  num_tasks = int(os.environ['SLURM_NTASKS'])

  # Resolve the cluster from SLURM environment
  cluster = tf.distribute.cluster_resolver.SlurmClusterResolver({"mesh": num_tasks},
								                                                port_base=8822,
                                                                gpus_per_node=FLAGS.gpus_per_node,
                                                                gpus_per_task=FLAGS.gpus_per_task,
                                                								tasks_per_node=FLAGS.tasks_per_node)
  cluster_spec = cluster.cluster_spec()
  # Create a server for all mesh members
  server = tf.distribute.Server(cluster_spec, "mesh", cluster.task_id)

  if cluster.task_id >0:
      server.join()

  # Otherwise we are the main task, let's define the devices
  devices = ["/job:mesh/task:%d/device:GPU:%d"%(i,j) for i in range(cluster_spec.num_tasks("mesh")) for j in range(FLAGS.gpus_per_task)]
  print("List of devices", devices)

  # And now a simple 2d mesh splitting the cubes along x and y dimensions
  mesh_shape = [("processor_rows", int(math.sqrt(len(devices)))),
                ("processor_cols", int(math.sqrt(len(devices))))]
  layout_rules = [("batch", "processor_rows"),
                  ("nx", "processor_cols")]

  # Instantiate the mesh implementation
  mesh_impl = mtf.placement_mesh_impl.PlacementMeshImpl(mesh_shape,
                                                        layout_rules, devices)

  # Create computational graphs
  initial_conditions, final_field, mesh_final_field = lpt_prototype(nc=FLAGS.nc,
                                                                    batch_size=FLAGS.batch_size,
                                                                    nproc=len(devices))
  # Lower mesh computation
  graph = mesh_final_field.graph
  mesh = mesh_final_field.mesh
  lowering = mtf.Lowering(graph, {mesh:mesh_impl})

  # Retrieve output of computation
  result = lowering.export_to_tf_tensor(mesh_final_field)

  with tf.Session(server.target) as sess:
    a,b,c = sess.run([initial_conditions, final_field, result])

  plt.figure(figsize=(15,3))
  plt.subplot(141)
  plt.imshow(a[0].sum(axis=2))
  plt.title('Initial Conditions')

  plt.subplot(142)
  plt.imshow(b[0].sum(axis=2))
  plt.title('TensorFlow (single GPU)')
  plt.colorbar()

  plt.subplot(143)
  plt.imshow(c[0].sum(axis=2))
  plt.title('Mesh TensorFlow')
  plt.colorbar()

  plt.subplot(144)
  plt.imshow((b[0] - c[0]).sum(axis=2))
  plt.title('Residuals')
  plt.colorbar()

  plt.savefig("comparison.png")

  exit(0)

if __name__ == "__main__":
  tf.app.run(main=main)