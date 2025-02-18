import forEach from 'lodash/forEach';
import set from 'lodash/set';

import {t} from 'sentry/locale';
import type {CustomRepo} from 'sentry/types/debugFiles';
import {CustomRepoType} from 'sentry/types/debugFiles';

export const customRepoTypeLabel = {
  [CustomRepoType.HTTP]: 'SymbolServer (HTTP)',
  [CustomRepoType.S3]: 'Amazon S3',
  [CustomRepoType.GCS]: 'Google Cloud Storage',
};

export const dropDownItems = [
  {
    value: CustomRepoType.S3,
    label: customRepoTypeLabel[CustomRepoType.S3],
    searchKey: t('aws amazon s3 bucket'),
  },
  {
    value: CustomRepoType.GCS,
    label: customRepoTypeLabel[CustomRepoType.GCS],
    searchKey: t('gcs google cloud storage bucket'),
  },
  {
    value: CustomRepoType.HTTP,
    label: customRepoTypeLabel[CustomRepoType.HTTP],
    searchKey: t('http symbol server ssqp symstore symsrv'),
  },
];

export function getRequestMessages(
  updatedRepositoriesQuantity: number,
  repositoriesQuantity: number
) {
  if (updatedRepositoriesQuantity > repositoriesQuantity) {
    return {
      successMessage: t('Successfully added custom repository'),
      errorMessage: t('An error occurred while adding a new custom repository'),
    };
  }

  if (updatedRepositoriesQuantity < repositoriesQuantity) {
    return {
      successMessage: t('Successfully removed custom repository'),
      errorMessage: t('An error occurred while removing the custom repository'),
    };
  }

  return {
    successMessage: t('Successfully updated custom repository'),
    errorMessage: t('An error occurred while updating the custom repository'),
  };
}

export function expandKeys(obj: CustomRepo) {
  const result: Record<string, string> = {};
  forEach(obj, (value, key) => {
    set(result, key.split('.'), value);
  });
  return result;
}
