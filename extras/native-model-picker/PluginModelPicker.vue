<script setup lang="ts">
import { ref, watch } from "vue";
import { pluginExtensionApi } from "@/api/v1";
import { useToast } from "@/utils/toast";

const props = defineProps<{
  modelValue: string;
  options: string[];
  pluginName: string;
  apiBase: string;
  apiKey: string;
}>();
const emit = defineEmits<{
  (event: "update:modelValue", value: string): void;
}>();
const toast = useToast();
const loading = ref(false);
const models = ref([...props.options]);

watch(
  () => props.options,
  (options) => {
    models.value = [...options];
  },
);

async function fetchModels() {
  loading.value = true;
  try {
    const response = await pluginExtensionApi.post<{
      models: { id: string }[];
    }>(`${props.pluginName}/models`, {
      api_base: props.apiBase,
      api_key: props.apiKey,
    });
    if (response.data.status === "error") {
      toast.error(response.data.message);
      return;
    }
    models.value = response.data.data.models.map((model) => model.id);
    if (models.value.length && !models.value.includes(props.modelValue)) {
      emit("update:modelValue", models.value[0]);
    }
    toast.success(`已获取 ${models.value.length} 个模型`);
  } catch (error) {
    toast.error(error instanceof Error ? error.message : "获取模型失败");
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div
    class="d-flex align-center ga-2"
    data-testid="native-plugin-model-picker"
  >
    <v-select
      :model-value="modelValue"
      :items="models"
      :disabled="loading"
      aria-label="Jev 模型"
      density="compact"
      variant="outlined"
      hide-details
      style="min-width: 0; flex: 1"
      @update:model-value="emit('update:modelValue', $event)"
    />
    <v-btn
      color="primary"
      variant="tonal"
      :loading="loading"
      @click="fetchModels"
    >
      获取模型
    </v-btn>
  </div>
</template>
