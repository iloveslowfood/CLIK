import os
import warnings
from abc import *
from typing import List, Union

import albumentations as A
import cv2
import numpy as np
import pandas as pd
import torch
from preprocessing import TextPreprocessor, remap_plan_keys, remap_prod_keys
from torch.utils.data import Dataset
from tqdm import tqdm

DEFAULT_PLAN_ATTRS = [
    "plan_name",
    "plan_startdate",
    "plan_cat1",
    "plan_cat2",
    "plan_kwds",
]
DEFAULT_PROD_ATTRS = [
    "prod_name",
    "prod_text",
    "prod_opendate",
    "prod_cat1",
    "prod_cat2",
    "prod_cat3",
    "prod_cat4",
    "prod_page_title",
]


class _PlanBaseDataset(Dataset, metaclass=ABCMeta):
    """네이버 쇼핑 기획전 전용 데이터셋

    <기획전 메타 데이터 구성 (column 구성)>
    I. 기획전 attribute (plan_attrs)
    - plan_id (int): 기획전 ID
    - plan_name (str): 기획전 제목
    - plan_startdate (str): 기획전 게시 날짜
    - plan_cat1 (str): 기획전 카테고리 (depth=1)
    - plan_cat2 (str): 기획전 카테고리 (depth=2)
    - plan_kwds (str): 기획전 키워드

    II. 상품 attribute (prod_attrs)
    - prod_id: (int): 상품 ID
    - prod_text (str): 상품 상세
    - prod_opendate (str): 상품 게시 날짜
    - prod_cat1 (str): 상품 카테고리 (depth=1)
    - prod_cat2 (str): 상품 카테고리 (depth=2)
    - prod_cat3 (str): 상품 카테고리 (depth=3)
    - prod_cat4 (str): 상품 카테고리 (depth=4)
    - prod_page_title (str): 상품 상세 페이지 제목

    III. Target: 랭킹 기준으로 삼을 점수
    - type 1. CTR: 상품별 클릭률
    - type 2. prod_review_cnt: 상품별 리뷰 수
    """

    def __init__(
        self,
        meta: pd.DataFrame,
        target: str,
        img_dir: str,
        img_transforms: A.Compose,
        txt_preprocessor: TextPreprocessor,
        plan_attrs: List[str] = None,
        prod_attrs: List[str] = None,
        sampling_method: str = "weighted",  # 'weighted', 'random', 'sequential'
    ):
        """
        Args:
            meta (pd.DataFrame): 메타 데이터
            target (str): compatibility target
            img_dir (str): directory of product images
            plan_attrs (List[str], optional): plan attributes used to train. Defaults to None.
            prod_attrs (List[str], optional): product attributes used to train. Defaults to None.
                                              NOTE. it can be used for text augmentation
            sampling_method (str, optional): [description]. Defaults to "weighted".
        """
        assert target in meta.columns, f"There's no '{target}' column in meta data"
        assert sampling_method in [
            "weighted",
            "random",
            "sequential",
        ], f"'sampling_method' should be one of 'weighted', 'random', 'sequential'"

        if plan_attrs is None:
            plan_attrs = DEFAULT_PLAN_ATTRS
        if prod_attrs is None:
            prod_attrs = DEFAULT_PROD_ATTRS
        assert set(plan_attrs).issubset(
            meta.columns.tolist()
        ), "There exists a column in 'plan_attrs' which is not in meta data."
        assert set(prod_attrs).issubset(
            meta.columns.tolist()
        ), "There exists a column in 'prod_attrs' which is not in meta data."

        # recompose meta data
        necessary_columns = (
            ["id", "prod_id", "plan_id", target] + plan_attrs + prod_attrs
        )
        meta = meta[sorted(set(necessary_columns))]

        self.meta = meta
        self.meta_access_by_plan_id = dict(tuple(meta.groupby("plan_id")))
        self.unique_plan_ids = self.meta["plan_id"].unique().tolist()

        self.img_transforms = img_transforms
        self.txt_preprocessor = txt_preprocessor
        self.plan_attrs = plan_attrs
        self.prod_attrs = prod_attrs
        self.img_dir = img_dir
        self.target = target
        self.sampling_method = sampling_method

        self.verify_meta_data()

    @abstractmethod
    def __getitem__(self):
        raise NotImplementedError

    @abstractmethod
    def __len__(self):
        raise NotImplementedError

    def load_prod_img(self, prod_id: int) -> np.array:
        img_path = os.path.abspath(os.path.join(self.img_dir, f"{prod_id}.jpg"))
        img = cv2.imread(img_path)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return img, img_path

    def verify_meta_data(self) -> bool:
        tqdm.pandas(desc="[Data Verification]")
        cannot_found_samples = self.meta[
            self.meta["prod_id"].progress_apply(
                lambda x: not os.path.isfile(os.path.join(self.img_dir, f"{x}.jpg"))
            )
        ]
        if len(cannot_found_samples) > 0:
            num_rows_origin = self.__len__()
            row_indices = cannot_found_samples.index.tolist()
            self.meta = self.meta.drop(row_indices, axis=0).reset_index(drop=True)
            num_rows_dropped = self.__len__()

            warnings.warn(
                f"There're samples whose images are not found. Dropped them to train properly. Data Size: {num_rows_origin:,d} -> {num_rows_dropped:,d}"
            )
            return False
        return True

    def sample_prods(
        self, plan: pd.DataFrame, n: int = 1
    ) -> Union[pd.Series, pd.DataFrame]:
        """한 기획전으로부터 n개 샘플을 추출

        Args:
            plan (pd.DataFrame): 한 기획전에 대한 데이터프레임
            n (int, optional): [description]. Defaults to 1.

        Returns:
            Union[pd.Series, pd.DataFrame]: [description]
        """
        if n == -1:
            prods = plan.sort_values(by=self.target, ascending=False, ignore_index=True)
            return prods

        elif n == 1:
            if self.sampling_method == "random":
                prod = plan.sample(1).squeeze()

            elif self.sampling_method == "weighted":
                # make weight with epsilon added to operate weighted sampling properly with uniquness
                plan["weight"] = (
                    plan[self.target].values + np.random.uniform(size=len(plan)) * 1e-8
                )

                # since result was not good if n=1, so sample positive with n=k iterations
                # NOTE. it is not that time consuming(+-1ns)
                prod = plan.sample(
                    n=7, weights="weight"
                )  # samping N(=7) times for weighted sampling properly
                prod = prod[prod["weight"] == prod["weight"].max()].drop(
                    "weight", axis=1
                )
                prod = prod.sample(1).squeeze()

            elif self.sampling_method == "sequential":
                prod = plan[plan[self.target] == plan[self.target].max()]
                prod = prod.sample(1).squeeze()

            else:
                raise NotImplementedError

            return prod

        elif n > 1:
            if len(plan) == n:
                prods = plan.sort_values(
                    by=self.target, ascending=False, ignore_index=True
                )
            elif self.sampling_method == "random":  # mainly for CM Task
                prods = plan.sample(n=n, replace=False)
                if self.target in prods.columns:
                    prods = prods.sort_values(
                        by=self.target, ascending=False, ignore_index=True
                    )
                else:
                    prods = prods.reset_index(drop=True)

            elif self.sampling_method == "weighted":  # mainly for CM Task
                # epsilon will be added to make weighted sampling work well
                plan["weight"] = (
                    plan[self.target].values + np.random.uniform(size=len(plan)) * 1e-8
                )
                # since result was not good if n=1, so sample positive with n=k iterations
                # NOTE. it is not that time consuming(+-1ns)
                pos_prod = plan.sample(n=5, weights="weight")
                pos_prod = pos_prod[
                    pos_prod["weight"] == pos_prod["weight"].max()
                ].sample(1)

                plan_except_pos = plan.drop(pos_prod.index.item(), axis=0)
                neg_prods = plan_except_pos.sample(
                    n=n - 1,
                    weights=plan_except_pos["weight"].max() - plan_except_pos["weight"],
                )
                prods = pd.concat([pos_prod, neg_prods], axis=0)
                prods = prods.sort_values(
                    by="weight", ascending=False, ignore_index=True
                ).drop("weight", axis=1)

            elif self.sampling_method == "sequential":
                plan = plan.sort_values(
                    by=self.target, ascending=False, ignore_index=True
                )
                prods = plan.head(n)

            else:
                raise NotImplementedError

            return prods


class SemanticMatchingDataset(_PlanBaseDataset):
    def __init__(
        self,
        meta: pd.DataFrame,
        target: str,
        img_dir: str,
        img_transforms: A.Compose,
        txt_preprocessor: TextPreprocessor,
        plan_attrs: List[str] = None,
        prod_attrs: List[str] = None,
        sampling_method: str = "random",
        p_txt_aug: float = 0.3,  # 텍스트를 상품 자체 텍스트 정보로 교체할 확률
    ):
        super(SemanticMatchingDataset, self).__init__(
            meta=meta,
            target=target,
            img_dir=img_dir,
            img_transforms=img_transforms,
            txt_preprocessor=txt_preprocessor,
            plan_attrs=plan_attrs,
            prod_attrs=prod_attrs,
            sampling_method=sampling_method,
        )
        self.p_txt_aug = p_txt_aug

    def __len__(self) -> int:
        return len(self.meta)

    def __getitem__(self, plan_id: int) -> dict:
        """
        prod: 상품과 소속 기획전 정보. 메타 데이터의 한 행
        plc: plan contents. 기획전 정보
        pri: product image. 상품 이미지
        """
        assert (
            plan_id in self.unique_plan_ids
        ), f"There's no plan_id '{plan_id}' in meta data"
        prod = self.sample_prods(self.meta_access_by_plan_id[plan_id])

        # text augmentation probabilistic
        if (self.prod_attrs is not None) and (np.random.binomial(1, p=self.p_txt_aug)):
            # 상품 attrs를 text input으로 활용할 경우
            txt_raw = prod[self.prod_attrs].to_dict()
            txt_contents = self.txt_preprocessor(**remap_prod_keys(txt_raw))
        else:
            # 기획전 attrs를 text input으로 활용할 경우
            txt_raw = prod[self.plan_attrs].to_dict()
            txt_contents = self.txt_preprocessor(**remap_plan_keys(txt_raw))

        # pri: product images
        pri, _ = self.load_prod_img(prod["prod_id"])
        pri = self.img_transforms(image=pri)["image"]

        return txt_contents, pri


class PreferenceDiscrimDataset(_PlanBaseDataset):
    def __init__(
        self,
        meta: pd.DataFrame,
        target: str,
        img_dir: str,
        img_transforms: A.Compose,
        txt_preprocessor: TextPreprocessor,
        plan_attrs: List[str],
        discrim_size: int = 30,
        sampling_method: str = "weighted",
    ):
        super(PreferenceDiscrimDataset, self).__init__(
            meta=meta,
            target=target,
            img_dir=img_dir,
            img_transforms=img_transforms,
            txt_preprocessor=txt_preprocessor,
            plan_attrs=plan_attrs,
            prod_attrs=None,
            sampling_method=sampling_method,
        )
        self.discrim_size = discrim_size

    def __len__(self):
        return len(self.unique_plan_ids)

    def __getitem__(self, plan_id: int) -> dict:
        """
        plan: 기획전 정보
        plc: plan contents. 기획전 정보
        pri: product image. 상품 이미지
        """
        assert (
            plan_id in self.unique_plan_ids
        ), f"There's no plan_id '{plan_id}' in meta data"
        plan = self.meta_access_by_plan_id[plan_id].reset_index(drop=True)

        plc_raw = plan.head(1)[self.plan_attrs].squeeze().to_dict()
        plc = self.txt_preprocessor(**remap_plan_keys(plc_raw))

        plan_prods = self.sample_prods(plan, n=self.discrim_size)
        pri = []
        for _, prod in plan_prods.iterrows():
            pri_raw, _ = self.load_prod_img(prod["prod_id"])
            pri.append(self.img_transforms(image=pri_raw)["image"])

        pri = torch.stack(pri)
        return plc, pri


class PDAblation2Dataset(_PlanBaseDataset):
    def __init__(
        self,
        meta: pd.DataFrame,
        target: str,
        img_dir: str,
        img_transforms: A.Compose,
        txt_preprocessor: TextPreprocessor,
        plan_attrs: List[str],
        discrim_size: int = 30,
        sampling_method: str = "weighted",
    ):
        super(PDAblation2Dataset, self).__init__(
            meta=meta,
            target=target,
            img_dir=img_dir,
            img_transforms=img_transforms,
            txt_preprocessor=txt_preprocessor,
            plan_attrs=plan_attrs,
            prod_attrs=None,
            sampling_method=sampling_method,
        )
        self.discrim_size = discrim_size

    def __len__(self):
        return len(self.unique_plan_ids)

    def __getitem__(self, plan_id: int) -> dict:
        """
        plan: 기획전 정보
        plc: plan contents. 기획전 정보
        pri: product image. 상품 이미지
        """
        assert (
            plan_id in self.unique_plan_ids
        ), f"There's no plan_id '{plan_id}' in meta data"
        plan = self.meta_access_by_plan_id[plan_id].reset_index(drop=True)

        plc_raw = plan.head(1)[self.plan_attrs].squeeze().to_dict()
        plc = self.txt_preprocessor(**remap_plan_keys(plc_raw))

        plan_prods = self.sample_prods(plan, n=self.discrim_size)
        pri, scores = [], []
        for _, prod in plan_prods.iterrows():
            pri_raw, _ = self.load_prod_img(prod["prod_id"])
            pri.append(self.img_transforms(image=pri_raw)["image"])
            scores.append(prod[self.target])

        pri = torch.stack(pri)
        return plc, pri, scores


class SemanticMatchingEvalDataset(_PlanBaseDataset):
    def __init__(
        self,
        meta: pd.DataFrame,
        target: str,
        img_dir: str,
        img_transforms: A.Compose,
        txt_preprocessor: TextPreprocessor,
        plan_attrs: List[str] = None,
        prod_attrs: List[str] = None,
        sampling_method: str = "random",
    ):
        super(SemanticMatchingEvalDataset, self).__init__(
            meta=meta,
            target=target,
            img_dir=img_dir,
            img_transforms=img_transforms,
            txt_preprocessor=txt_preprocessor,
            plan_attrs=plan_attrs,
            prod_attrs=prod_attrs,
            sampling_method=sampling_method,
        )

    def __len__(self) -> int:
        return len(self.meta)

    def __getitem__(self, plan_id: int) -> dict:
        """
        prod: 상품과 소속 기획전 정보. 메타 데이터의 한 행
        plc: plan contents. 기획전 정보
        pri: product image. 상품 이미지
        """
        assert (
            plan_id in self.unique_plan_ids
        ), f"There's no plan_id '{plan_id}' in meta data"
        desc = dict(
            id=[], plan_id=None, prod_id=[], prod_score=[], pri_path=[], plan_attrs=None
        )

        prod = self.sample_prods(self.meta_access_by_plan_id[plan_id])

        plc_raw = prod[self.plan_attrs].to_dict()
        plc = self.txt_preprocessor(**remap_plan_keys(plc_raw))

        pri, pri_path = self.load_prod_img(prod["prod_id"])
        pri = self.img_transforms(image=pri)["image"]

        desc["plan_attrs"] = plc_raw
        desc["plan_id"] = plan_id
        desc["id"] = prod["id"]
        desc["prod_id"] = prod["prod_id"]
        desc["prod_score"] = prod[self.target]
        desc["pri_path"] = pri_path

        return plc, pri, desc


class PreferenceDiscrimEvalDataset(_PlanBaseDataset):
    def __init__(
        self,
        meta: pd.DataFrame,
        target: str,
        img_dir: str,
        img_transforms: A.Compose,
        txt_preprocessor: TextPreprocessor,
        plan_attrs: List[str],
        discrim_size: int = 30,
        sampling_method: str = "weighted",
    ):
        super(PreferenceDiscrimEvalDataset, self).__init__(
            meta=meta,
            target=target,
            img_dir=img_dir,
            img_transforms=img_transforms,
            txt_preprocessor=txt_preprocessor,
            plan_attrs=plan_attrs,
            prod_attrs=None,
            sampling_method=sampling_method,
        )
        self.discrim_size = discrim_size

    def __len__(self):
        return len(self.unique_plan_ids)

    def __getitem__(self, plan_id: int) -> dict:
        """
        plan: 기획전
        plc: plan contents. 기획전 정보
        pri: product image. 상품 이미지

        Returns:
            dict: [description]
        """
        assert (
            plan_id in self.unique_plan_ids
        ), f"There's no plan_id '{plan_id}' in meta data"
        desc = dict(
            id=[], plan_id=None, prod_id=[], prod_score=[], pri_path=[], plan_attrs=None
        )
        plan = self.meta_access_by_plan_id[plan_id].reset_index(drop=True)

        plc_raw = plan.head(1)[self.plan_attrs].squeeze().to_dict()
        plc = self.txt_preprocessor(**remap_plan_keys(plc_raw))

        desc["plan_id"] = plan_id
        desc["plan_attrs"] = plc_raw

        prods = self.sample_prods(plan, n=self.discrim_size)
        pri = []
        for _, row in prods.iterrows():
            prod_id = row["prod_id"]

            pri_raw, pri_path = self.load_prod_img(prod_id)
            pri.append(self.img_transforms(image=pri_raw)["image"])

            desc["id"].append(row["id"])
            desc["prod_id"].append(prod_id)
            desc["prod_score"].append(row[self.target])
            desc["pri_path"].append(pri_path)

        pri = torch.stack(pri)
        return plc, pri, desc
