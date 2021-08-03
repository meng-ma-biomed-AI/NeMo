set -e

dataset_dir="$1"
asr_model="$2"  # Path to checkpoint or NGC pretrained name
translation_model="$3"
output_dir="$4"
segmented="$5"  # 1 or 0
mwerSegmenter="$6"  # 1 or 0


audio_dir="${dataset_dir}/wavs"
asr_model_name="$(basename "${asr_model}")"
translation_model_name=$(basename "${translation_model}")

en_ground_truth_manifest="${output_dir}/en_ground_truth_manifest.json"


if [ "${asr_model: -5}" -eq ".nemo" ]; then
  asr_model_argument_name=model_path
else
  asr_model_argument_name=pretrained_name
fi


if [ "${translation_model: -5}" -eq ".nemo" ]; then
  translation_model_parameter="-p"
else
  translation_model_parameter="-m"
fi


printf "Creating IWSLT manifest.."
python create_iwslt_manifest.py -a "${audio_dir}" \
  -t "${dataset_dir}/IWSLT.TED.tst2019.en-de.en.xml" \
  -o "${en_ground_truth_manifest}"


if [ "${segmented}" -eq 1 ]; then
  printf "\nSplitting audio files.."
  split_data_path="${output_dir}/split"
  python iwslt_split_audio.py -a "${dataset_dir}/wavs" \
    -s "${dataset_dir}/IWSLT.TED.tst2019.en-de.yaml" \
    -d "${split_data_path}"
  fi
  split_transcripts="${dataset_dir}/split_transcripts/${asr_model_name}"
  transcript="${output_dir}/transcripts_segmented_input/${asr_model_name}.manifest"
  mkdir -p "${output_dir}/transcripts_segmented_input"
  for f in "${split_data_path}"/*; do
    talk_id=$(basename "${f}")
    if [[ "${talk_id}" =~ ^[1-9][0-9]*$ ]]; then
      python ~/NeMo/examples/asr/transcribe_speech.py "${asr_model_argument_name}"="${asr_model}" \
        audio_dir="${f}" \
        output_filename="${split_transcripts}/${talk_id}.manifest" \
        cuda=true \
        batch_size=4
    fi
  done
  python join_split_wav_manifests.py -S "${split_transcripts}" -o "${transcript}" -n "${audio_dir}"
else
  if [ "${segmented}" -ne 0 ]; then
    echo "Wrong value '${segmented}' of fifth parameter of 'translate_and_score.sh'. Only '0' and '1' are supported."
    exit 1
  fi
  transcript="${output_dir}/transcripts_not_segmented_input/${asr_model_name}.manifest"
  mkdir -p "${output_dir}/transcripts_not_segmented_input"
  python ~/NeMo/examples/asr/transcribe_speech.py "${asr_model_argument_name}"="${asr_model}" \
    audio_dir="${audio_dir}" \
    output_filename="${transcript}" \
    cuda=true \
    batch_size=1
fi


printf "\nComputing WER.."
wer_by_transcript_and_audio="${output_dir}/wer_by_transcript_and_audio"
if [ "${segmented}" -eq 1 ]; then
  wer_dir="segmented"
else
  wer_dir="not_segmented"
fi
wer="$(python wer_between_2_manifests.py "${transcript}" "${en_ground_truth_manifest}" \
      -o "${wer_by_transcript_and_audio}/${wer_dir}/${asr_model_name}.json")"
echo "WER: ${wer}"


printf "\nAdding punctuation and restoring capitalization.."
if [ "${segmented}" -eq 1 ]; then
  punc_dir="${output_dir}/punc_transcripts_segmented_input"
else
  punc_dir="${output_dir}/punc_transcripts_not_segmented_input"
fi
python punc_cap.py -a "${en_ground_truth_manifest}" \
  -p "${transcript}" \
  -o "${punc_dir}/${asr_model_name}.txt"


printf "\nTranslating.."
if [ "${segmented}" -eq 1 ]; then
  translation_dir="${output_dir}/${translations_segmented_input}"
else
  translation_dir="${output_dir}/${translations_not_segmented_input}"
fi
translated_text="${translation_dir}/${translation_model_name}/${asr_model_name}.txt"
python translate_iwslt.py "${translation_model_parameter}" "${translation_model}" \
  -i "${punc_dir}/${asr_model_name}.txt" \
  -o "${translated_text}"
  -s


if [ "${mwerSegmenter}" -eq 1 ]; then
  printf "\nSegmenting translations using mwerSegmenter.."
  conda activate mwerSegmenter  # python 2 conda environment
  cd ~/mwerSegmenter/
  if [ "${segmented}" -eq 1 ]; then
    translation_dir_mwer_xml="${output_dir}/${mwer_translations_xml_segmented_input}"
    translation_dir_mwer_txt="${output_dir}/${mwer_translations_txt_segmented_input}"
  else
    translation_dir_mwer_xml="${output_dir}/${mwer_translations_xml_not_segmented_input}"
    translation_dir_mwer_txt="${output_dir}/${mwer_translations_txt_not_segmented_input}"
  fi
  translated_mwer_xml="${translation_dir_mwer_xml}/${translation_model_name}/${asr_model_name}.xml"
  mkdir -p "$(dirname "${translated_mwer_xml}")"
  translated_text_for_scoring="${translation_dir_mwer_txt}/${translation_model_name}/${asr_model_name}.txt"
  ./segmentBasedOnMWER.sh "${workdir}/IWSLT.TED.tst2019.en-de.en.xml" \
    "${dataset_dir}/IWSLT.TED.tst2019.en-de.de.xml" \
    "${translated_text}" \
    "${asr_model_name}" \
    German \
    "${translated_mwer_xml}" \
    no \
    1
  cd -
  conda deactivate
  reference="${output_dir}/iwslt_de_text_by_segs.txt"
  python xml_2_text_segs_2_lines.py -i "${dataset_dir}/IWSLT.TED.tst2019.en-de.de.xml" -o "${reference}"
  mkdir -p "$(dirname "${translated_text_for_scoring}")"
  python xml_2_text_segs_2_lines.py -i "${translated_mwer_xml}" -o "${translated_text_for_scoring}"
else
  if [ "${segmented}" -ne 0 ]; then
    echo "Wrong value '${mwerSegmenter}' of sixth parameter of 'translate_and_score.sh'. Only '0' and '1' are supported."
    exit 1
  fi
  translated_text_for_scoring="${translated_text}"
  reference="${output_dir}/iwslt_de_text_by_wavs.txt"
  python prepare_iwslt_text_for_translation.py -a "${en_ground_truth_manifest}" \
    -t "${dataset_dir}/IWSLT.TED.tst2019.en-de.de.xml" \
    -o "${reference}"
fi


printf "\nComputing BLEU.."
bleu=$(sacrebleu "${reference}" -i "${translated_text_for_scoring}" -m bleu -b -w 4)
echo "BLEU: ${bleu}"
output_file="${output_dir}/BLEU.txt"
echo "" >> "${output_file}"
echo "ASR model: ${asr_model}" >> "${output_file}"
echo "NMT model: ${translation_model}" >> "${output_file}"
echo "BLUE: ${bleu}" >> "${output_file}"

set +e