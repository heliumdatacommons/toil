cwlVersion: v1.0
class: Workflow
id: complex-workflow
inputs:
  archive:
    type: string
  touch_files:
    type:
      type: array
      items: string
  echo_message:
    type: string
  echo_output_location:
    type: string

outputs:
  output_archive:
    type: File
    outputSource: tar_step/archive_out

steps:

  touch_step:
    run: touch.cwl
    scatter: filename
    scatterMethod: dotproduct
    in:
      filename:
        source: "#touch_files"
    out: [file_out]

  echo_step:
    run: echo.cwl
    in:
      message: "#echo_message"
      output_location: "#echo_output_location"
    out: [echo_output]

  tar_step:
    run: tar.cwl
    in:
      archive_file:
        source: "#archive"
      file_list:
        source: ["#touch_step/file_out", "#echo_step/echo_output"]
        linkMerge: merge_flattened
    out: [archive_out]


requirements:
  - class: StepInputExpressionRequirement
  - class: ScatterFeatureRequirement
  - class: MultipleInputFeatureRequirement

